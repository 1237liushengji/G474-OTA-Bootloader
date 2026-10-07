/**
  ******************************************************************************
  * @file    bl_diag.c
  * @brief   BL 的 UDS 服务子集 + 下载传输状态机实现（字节级契约）
  *
  * 支持的服务（与 APP 侧完全同构，上位机不需要区分 BL/APP）：
  *   0x10 会话控制（默认 0x01 / 扩展 0x03；正响应固定 50 <sub> 00 32 01 F4）
  *   0x11 ECU 复位（仅 01；先发 51 01，等应答真正上线后 ~50ms 再 NVIC_SystemReset）
  *   0x22 读 DID（F180 状态 / F181 活动槽 / F182 版本 / F183 双槽 CRC / F184 能力 0x07）
  *   0x2E 写 F190（BL 中仅记录并回 6E F1 90）
  *   0x31 例程 FF00 擦除 / FF01 校验 / FF02 激活 / FF03 回滚（FF04 属 APP，BL 回 NRC 0x12）
  *   0x34 请求下载 / 0x36 传输数据 / 0x37 退出传输
  *   0x3E TesterPresent（3E 00 -> 7E 00；3E 80 无应答）
  *   其余 SID -> NRC 0x11；长度不符 -> NRC 0x13
  *
  * 会话与 S3：只有扩展会话才允许 0x34/0x36/0x37/0x31/0x2E/0x11，默认会话回 NRC 0x22；
  *          任何有效请求刷新 S3，5s 无请求自动回默认会话。
  *
  * 下载数据通路（模式 A：设备侧合成镜像头，契约 ota_common.h"刷写地址与续传约定"）：
  *   0x34 addr = 槽 APP 链接基址（= 槽基址 + OTA_IMG_HDR_SIZE，当前 = 槽基址 + 0x80，
  *   **不是 +0x20**，见血线 12），支持续传（addr 在槽净荷范围内任意
  *   8 字节对齐位置）；size = 本次要收的净固件字节数。
  *   0x36 的净荷累积进 2KB 页缓冲，满 2048 字节即 BL_FlashWrite 落盘，同时累计长度与 CRC32。
  *   0x37 把残余字节以 0xFF 补齐落盘（BL_FlashWrite 内部按双字 0xFF 补齐），可携带
  *   4 字节大端整镜像 CRC32（[37, crc32(4B)]）作为 FF01 与合成头的参考值。
  *   FF02 激活：设备在槽首合成 32B 镜像头（magic/imgSize/payloadSize/loadAddr/payloadCrc/
  *   hdrCrc），并**回读校验**（调用 BL_ImageValidate，含净固件 CRC 与 SP/复位向量），
  *   校验通过才写配置 state=TRIAL_<目标槽> 并回 0x00，否则回 0x01。
  *
  * 保护：擦除/写入目标恒为"活动槽之外的另一个槽"，且都经 BL_FlashEraseRange/BL_FlashWrite
  *       的 addr < OTA_SLOT_A_ADDR 硬保护（BL 向量表页 + 控制区 + BL 代码区绝不可触碰）。
  ******************************************************************************
  */
#include "bl_diag.h"
#include "bl_isotp.h"
#include "bl_flash_if.h"
#include "bl_crc32.h"
#include "bl_param.h"
#include "bl_image.h"
#include "bl_can.h"
#include "bl_uart.h"
#include <string.h>

/*--------------------------------------- 会话与常量 --------------------------------------*/
#define BL_DIAG_SESSION_DEFAULT     0x01U
#define BL_DIAG_SESSION_EXTENDED    0x03U
#define BL_DIAG_S3_TIMEOUT_MS       5000U

#define BL_DIAG_PAGE_SIZE           OTA_FLASH_PAGE_SIZE            /* 2KB 页缓冲 */
#define BL_DIAG_MAX_BLOCK_LEN       UDS_MAXLEN_FIELD               /* 0x34 应答通告的 maxLen = UDS_DL_MAX_BLOCK_PAYLOAD = 1016（8 字节倍数，续传才可用） */

/* 0x31 01 FF01 正应答长度（契约 UDS_VERIFY_RESP_FULL_LEN = 9） */
#define BL_DIAG_VERIFY_RESP_LEN     UDS_VERIFY_RESP_FULL_LEN
#define BL_DIAG_VERIFY_ST_OK        0x00U
#define BL_DIAG_VERIFY_ST_CRC       0x01U
#define BL_DIAG_VERIFY_ST_LEN       0x02U

/* 契约一致性编译期锁死：块长上限、0x37 携带 CRC 的约定被改动时必须报错而不是静默跑偏 */
#if (UDS_MAXLEN_FIELD < 125U)
#error "UDS_MAXLEN_FIELD 必须 >= 125（契约要求 0x34 通告的 maxLen 至少 125）"
#endif
#if (UDS_DL_MAX_BLOCK_PAYLOAD != UDS_MAXLEN_FIELD)
#error "契约 UDS_DL_MAX_BLOCK_PAYLOAD 必须等于 UDS_MAXLEN_FIELD（块长上限与通告值一致）"
#endif
#if (UDS_XFEREXIT_WITH_CRC != 1U)
#error "本实现按 UDS_XFEREXIT_WITH_CRC=1 实现 0x37 携带 4 字节 CRC（[37, crc32(4B)]）"
#endif

/*--------------------------------------- 下载状态机 --------------------------------------*/
typedef struct
{
  bool     active;          /* 0x34/FF00 之后事务进行中 */
  bool     complete;        /* 0x37 已收到 */
  uint8_t  targetSlot;      /* 目标槽（恒为活动槽之外的另一个） */
  uint32_t slotBase;        /* 目标槽基址 */
  uint32_t appBase;         /* 目标槽 APP 链接基址 = slotBase + OTA_IMG_HDR_SIZE（0x80，非 32，见血线 12） */
  uint32_t startAddr;       /* 本次落盘起始地址（续传时 != appBase） */
  uint32_t declaredSize;    /* 本次要接收的固件字节数 */
  uint32_t receivedLen;     /* 已接收净荷字节数 */
  uint32_t blockSize;       /* 本轮协商的块净荷大小（0x34 通告值，用于反推绝对块号） */
  uint32_t firstBlockIndex; /* 本轮首块的**绝对**块号（1 起）。全新下载=1；续传=偏移反推。
                             * 契约规定 seq = ((绝对块号-1) % 255)+1，若续传时误当第 1 块算，
                             * 上位机按绝对块号发 seq 就会全部不匹配 -> 必回 NRC 0x73。 */
  uint32_t blockCount;      /* 本轮已接收块数（用于计算期望序号 UDS_BLOCK_SEQ_OF） */
  uint32_t runCrc;          /* 接收流的流式 CRC 中间值 */
  uint32_t refCrc;          /* 0x37 携带的参考 CRC32 */
  bool     refCrcValid;
  uint32_t pageFill;        /* 页缓冲内字节数 */
  uint32_t streamAddr;      /* 下一个落盘地址 */
} BL_DiagDl;

/*--------------------------------------- 模块内部变量 ------------------------------------*/
static uint8_t    s_pageBuf[BL_DIAG_PAGE_SIZE];   /* 2KB 页缓冲（static：RAM 2KB） */
static BL_DiagDl  s_dl;
static uint8_t    s_session;
static uint32_t   s_lastActivity;
static uint8_t    s_activeSlot;
static bool       s_bootRequested;
static uint32_t   s_progErrors;                   /* 落盘失败次数（诊断计数） */

/*--------------------------------------- 小工具 ------------------------------------------*/
static uint16_t BL_DiagBe16(const uint8_t *p)
{
  return (uint16_t)(((uint16_t)p[0] << 8) | (uint16_t)p[1]);
}

static uint32_t BL_DiagBe32(const uint8_t *p)
{
  return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) | ((uint32_t)p[2] << 8) | (uint32_t)p[3];
}

static void BL_DiagSend(const uint8_t *resp, uint16_t len)
{
  if (BL_IsoTpSend(resp, len) != BL_ISOTP_OK)
  {
    BL_UART_SEND_STR("BL: diag resp dropped (isotp busy)\r\n");
  }
}

static void BL_DiagSendNrc(uint8_t sid, uint8_t nrc)
{
  uint8_t resp[3];

  resp[0] = 0x7FU;
  resp[1] = sid;
  resp[2] = nrc;
  BL_DiagSend(resp, 3U);
}

/**
  * @brief  会话门禁：非扩展会话回 NRC 0x22
  */
static bool BL_DiagRequireExtended(uint8_t sid)
{
  if (s_session != BL_DIAG_SESSION_EXTENDED)
  {
    BL_DiagSendNrc(sid, UDS_NRC_CONDITIONS);
    return false;
  }
  return true;
}

/**
  * @brief  state -> TRIAL_<slot> / IDLE_<slot>
  */
static uint32_t BL_DiagTrialStateOf(uint8_t slot)
{
  return (slot == 0U) ? OTA_ST_TRIAL_A : OTA_ST_TRIAL_B;
}

static uint32_t BL_DiagIdleStateOf(uint8_t slot)
{
  return (slot == 0U) ? OTA_ST_IDLE_A : OTA_ST_IDLE_B;
}

/**
  * @brief  期望的 0x36 块序号：1..255 循环、255 之后回 1，**0 永远非法**
  * @note   契约 UDS_BLOCK_SEQ_OF(idx) = ((idx-1) % 255) + 1；本会话首块 idx=1 -> 1
  */
static uint8_t BL_DiagExpectedSeq(void)
{
  return UDS_BLOCK_SEQ_OF(s_dl.firstBlockIndex + s_dl.blockCount);
}

/**
  * @brief  记录下载失败原因到 cfg.lastError（现场可诊断）
  */
static void BL_DiagRecordError(uint32_t otaErr)
{
  OTA_Cfg cfg = *BL_ParamGet();

  if (cfg.lastError != otaErr)
  {
    cfg.lastError = otaErr;
    if (!BL_ParamStore(&cfg))
    {
      BL_UART_SEND_STR("BL: cfg store FAIL\r\n");
    }
  }
}

/*--------------------------------------- 下载准备/落盘 -----------------------------------*/
/**
  * @brief  初始化（或复位）下载事务并擦除目标槽所需页
  * @param  addr 本次净荷起始地址（0x34 的 addr）
  * @param  size 本次净荷字节数
  * @retval true=擦除成功且事务已就绪
  * @note   擦除自包含 addr 的整页开始、向上取整到页：
  *         - 全新下载 addr=槽APP基址(槽基址+OTA_IMG_HDR_SIZE) -> 起始页 = 槽基址所在页，
  *           正好把槽首 0x80 保留区（含 32B 镜像头）一并擦干净（FF02 要在那里合成头）；
  *         - 续传 addr 为后续位置 -> 只擦 addr 所在页及其后，不重擦前面的页。
  *
  *         ⚠️ firstBlockIndex 必须由 (addr - appBase) 反推**绝对块号**（契约
  *         ota_common.h 的块序号约定）。曾无条件清零，导致 --resume N 时上位机
  *         按绝对块号发 seq=N、设备却期望 seq=1 -> 续传必回 NRC 0x73。
  *         BL_DiagRangeOk 已保证 (addr - appBase) 为块大小整数倍（块大小是 8 的倍数、
  *         addr 8 对齐、appBase 0x80 对齐），故此处整除必然整除。
  */
static bool BL_DiagPrepareDownload(uint32_t addr, uint32_t size)
{
  uint32_t eraseStart;
  uint32_t eraseSize;
  uint32_t offset;

  s_dl.targetSlot   = (uint8_t)(1U - s_activeSlot);
  s_dl.slotBase     = (s_dl.targetSlot == 0U) ? OTA_SLOT_A_ADDR : OTA_SLOT_B_ADDR;
  s_dl.appBase      = s_dl.slotBase + OTA_IMG_HDR_SIZE;
  s_dl.startAddr    = addr;
  s_dl.declaredSize = size;
  s_dl.receivedLen  = 0U;
  s_dl.blockSize    = BL_DIAG_MAX_BLOCK_LEN;
  offset            = addr - s_dl.appBase;        /* BL_DiagRangeOk 已保证 >= appBase */
  s_dl.firstBlockIndex = (offset / s_dl.blockSize) + 1U;   /* 绝对块号，1 起 */
  s_dl.blockCount   = 0U;
  s_dl.runCrc       = BL_Crc32Init();
  s_dl.refCrc       = 0U;
  s_dl.refCrcValid  = false;
  s_dl.pageFill     = 0U;
  s_dl.streamAddr   = addr;
  s_dl.complete     = false;
  s_dl.active       = false;

  BL_UartPrintf("BL: dl slot %u appBase 0x%08lX off %lu -> firstBlock %lu\r\n",
                (unsigned)s_dl.targetSlot,
                (unsigned long)s_dl.appBase,
                (unsigned long)offset,
                (unsigned long)s_dl.firstBlockIndex);

  /* 擦除范围的起点对齐到页（Flash 只能整页擦） */
  eraseStart = addr & ~(OTA_FLASH_PAGE_SIZE - 1U);
  eraseSize  = (addr + size) - eraseStart;         /* 覆盖到净荷末尾 */

  BL_UartPrintf("BL: erase slot %u from 0x%08lX len 0x%lX\r\n",
                (unsigned)s_dl.targetSlot,
                (unsigned long)eraseStart,
                (unsigned long)eraseSize);

  if (BL_FlashEraseRange(eraseStart, eraseSize) != OTA_FLASH_OK)
  {
    BL_UART_SEND_STR("BL: erase FAIL\r\n");
    BL_DiagRecordError(OTA_ERR_ERASE);
    return false;
  }

  s_dl.active = true;
  return true;
}

/**
  * @brief  把一段净荷追加进页缓冲，满 2048 字节即落盘
  * @retval true=成功；false=落盘失败（事务已被置为不可用）
  */
static bool BL_DiagAppendData(const uint8_t *data, uint32_t len)
{
  uint32_t offset = 0U;

  s_dl.runCrc = BL_Crc32Update(s_dl.runCrc, data, len);

  while (offset < len)
  {
    uint32_t space = BL_DIAG_PAGE_SIZE - s_dl.pageFill;
    uint32_t n     = ((len - offset) < space) ? (len - offset) : space;

    (void)memcpy(&s_pageBuf[s_dl.pageFill], &data[offset], (size_t)n);
    s_dl.pageFill += n;
    offset        += n;

    if (s_dl.pageFill >= BL_DIAG_PAGE_SIZE)
    {
      if (BL_FlashWrite(s_dl.streamAddr, s_pageBuf, BL_DIAG_PAGE_SIZE) != OTA_FLASH_OK)
      {
        s_progErrors++;
        BL_UartPrintf("BL: flash program FAIL @0x%08lX\r\n", (unsigned long)s_dl.streamAddr);
        BL_DiagRecordError(OTA_ERR_PROGRAM);
        s_dl.active = false;
        return false;
      }
      s_dl.streamAddr += BL_DIAG_PAGE_SIZE;
      s_dl.pageFill    = 0U;
    }
  }

  s_dl.receivedLen += len;
  return true;
}

/**
  * @brief  0x31 01 FF00 / 0x34 共用的目标槽合法区间检查
  * @retval true=addr/size 落在目标槽净荷区内
  */
static bool BL_DiagRangeOk(uint32_t addr, uint32_t size)
{
  uint32_t appBase = ((1U - s_activeSlot) == 0U) ? (OTA_SLOT_A_ADDR + OTA_IMG_HDR_SIZE)
                                                 : (OTA_SLOT_B_ADDR + OTA_IMG_HDR_SIZE);

  if (size == 0U)
  {
    return false;
  }
  if ((addr % 8U) != 0U)
  {
    return false;                                 /* 双字编程：续传地址必须 8 字节对齐 */
  }
  if (addr < appBase)
  {
    return false;
  }
  if (size > (uint32_t)OTA_IMG_MAX_PAYLOAD)
  {
    return false;
  }
  if ((addr - appBase) > ((uint32_t)OTA_IMG_MAX_PAYLOAD - size))
  {
    return false;                                 /* 末尾越出槽净荷区 */
  }
  return true;
}

/*--------------------------------------- 0x10 / 0x11 ------------------------------------*/
static void BL_DiagService10(const uint8_t *req, uint16_t len)
{
  uint8_t sub;
  uint8_t resp[6];

  if (len != 2U)
  {
    BL_DiagSendNrc(UDS_SID_DIAG_SESSION, UDS_NRC_INCORRECT_MSG_LEN);
    return;
  }

  /* 屏蔽 bit7（suppressPosRspMsg）取子功能号 */
  sub = (uint8_t)(req[1] & UDS_SUBFUNC_MASK);
  if ((sub != BL_DIAG_SESSION_DEFAULT) && (sub != BL_DIAG_SESSION_EXTENDED))
  {
    BL_DiagSendNrc(UDS_SID_DIAG_SESSION, UDS_NRC_SUBFUNC_NOT_SUP);
    return;
  }

  s_session      = sub;
  s_lastActivity = HAL_GetTick();                 /* S3 重新计时 */

  resp[0] = 0x50U;
  resp[1] = sub;
  resp[2] = 0x00U;                                /* P2 = 50ms */
  resp[3] = 0x32U;                                /* P2* = 0x0132 * 10ms = 5000ms */
  resp[4] = 0x01U;
  resp[5] = 0xF4U;
  BL_DiagSend(resp, 6U);
}

static void BL_DiagService11(const uint8_t *req, uint16_t len)
{
  uint8_t resp[2];
  uint32_t start;

  if (len != 2U)
  {
    BL_DiagSendNrc(UDS_SID_ECU_RESET, UDS_NRC_INCORRECT_MSG_LEN);
    return;
  }
  if (req[1] != 0x01U)
  {
    BL_DiagSendNrc(UDS_SID_ECU_RESET, UDS_NRC_SUBFUNC_NOT_SUP);
    return;
  }
  /* 复位属破坏性操作：与 docs/03 一致，要求扩展会话 */
  if (!BL_DiagRequireExtended(UDS_SID_ECU_RESET))
  {
    return;
  }

  resp[0] = 0x51U;
  resp[1] = 0x01U;
  BL_DiagSend(resp, 2U);

  /* 先确保应答真的发上总线，再等 ~50ms 让上位机收完，最后软复位 */
  start = HAL_GetTick();
  while ((!BL_CanTxIdle()) && ((HAL_GetTick() - start) < 50U))
  {
    /* 等 Tx FIFO 排空（最多 50ms） */
  }
  HAL_Delay(50U);
  BL_UART_SEND_STR("BL: ECU reset (11 01)\r\n");
  NVIC_SystemReset();

  /* NVIC_SystemReset 不返回 */
}

/*--------------------------------------- 0x22 --------------------------------------------*/
static void BL_DiagService22(const uint8_t *req, uint16_t len)
{
  const OTA_Cfg *cfg = BL_ParamGet();
  uint8_t        resp[12];
  uint16_t       rlen = 0U;
  uint16_t       did;

  if (len != 3U)
  {
    BL_DiagSendNrc(UDS_SID_READ_DID, UDS_NRC_INCORRECT_MSG_LEN);
    return;
  }

  did = BL_DiagBe16(&req[1]);

  resp[rlen] = 0x62U;
  rlen++;
  resp[rlen] = req[1];
  rlen++;
  resp[rlen] = req[2];
  rlen++;

  switch (did)
  {
    case UDS_DID_OTA_STATE:                       /* F180：1 字节状态 */
      resp[rlen] = (uint8_t)(cfg->state & 0xFFU);
      rlen++;
      break;

    case UDS_DID_ACTIVE_SLOT:                     /* F181：1 字节活动槽 */
      resp[rlen] = s_activeSlot;
      rlen++;
      break;

    case UDS_DID_OTA_VERSION:                     /* F182：A 版本 2B + B 版本 2B */
      resp[rlen] = (uint8_t)((cfg->slotVersion[0] >> 24) & 0xFFU);
      rlen++;
      resp[rlen] = (uint8_t)((cfg->slotVersion[0] >> 16) & 0xFFU);
      rlen++;
      resp[rlen] = (uint8_t)((cfg->slotVersion[1] >> 24) & 0xFFU);
      rlen++;
      resp[rlen] = (uint8_t)((cfg->slotVersion[1] >> 16) & 0xFFU);
      rlen++;
      break;

    case UDS_DID_SLOT_CRC:                        /* F183：A CRC(4B 大端) + B CRC */
    {
      uint8_t i;

      for (i = 0U; i < 2U; i++)
      {
        resp[rlen] = (uint8_t)((cfg->slotCrc[i] >> 24) & 0xFFU);
        rlen++;
        resp[rlen] = (uint8_t)((cfg->slotCrc[i] >> 16) & 0xFFU);
        rlen++;
        resp[rlen] = (uint8_t)((cfg->slotCrc[i] >> 8) & 0xFFU);
        rlen++;
        resp[rlen] = (uint8_t)(cfg->slotCrc[i] & 0xFFU);
        rlen++;
      }
      break;
    }

    case UDS_DID_CAPABILITY:                      /* F184：能力位图固定 0x07 */
      resp[rlen] = 0x07U;
      rlen++;
      break;

    default:
      BL_DiagSendNrc(UDS_SID_READ_DID, UDS_NRC_REQUEST_RANGE);
      return;
  }

  BL_DiagSend(resp, rlen);
}

/*--------------------------------------- 0x2E --------------------------------------------*/
static void BL_DiagService2E(const uint8_t *req, uint16_t len)
{
  uint8_t resp[3];
  uint16_t did;

  if (!BL_DiagRequireExtended(UDS_SID_WRITE_DID))
  {
    return;
  }
  if (len != 4U)
  {
    BL_DiagSendNrc(UDS_SID_WRITE_DID, UDS_NRC_INCORRECT_MSG_LEN);
    return;
  }

  did = BL_DiagBe16(&req[1]);
  if ((did != UDS_DID_BOOT_REQ) || (req[3] != 0x01U))
  {
    BL_DiagSendNrc(UDS_SID_WRITE_DID, UDS_NRC_REQUEST_RANGE);
    return;
  }

  /* BL 中该请求"仅记录"：真正的"下次复位进 BL"语义由 APP 侧实现并持久化 */
  s_bootRequested = true;
  BL_UART_SEND_STR("BL: boot request recorded (2E F190)\r\n");

  resp[0] = 0x6EU;
  resp[1] = 0xF1U;
  resp[2] = 0x90U;
  BL_DiagSend(resp, 3U);
}

/*--------------------------------------- 0x31 例程 ---------------------------------------*/
/**
  * @brief  FF00 擦除目标槽并初始化下载
  * @param  req 请求体：31 01 FF 00 [size(2B 大端)]
  * @param  len 4 字节（不带 size，擦整槽）或 6 字节（带 size）
  *
  * @note   ⚠️ 本例程**是冗余路径**，上位机 ota_host.py 已不再调用：
  *           - 0x34 的 BL_DiagPrepareDownload 已隐式无条件擦除目标槽；
  *           - size 字段只有 16 位，上限 65535 < 单槽净荷 212864，**无法表达整槽擦除**。
  *         保留它只为兼容可能仍发送该请求的老工具；新工具应直接走 0x34。
  *
  * @note   2026-10-07 修正：曾硬性要求 len == 6，而上位机发的是 4 字节
  *         （`31 01 FF 00`），导致每次整槽刷写都在第一步就被回 NRC 0x13 中断。
  *         现同时接受 4/6 字节，长度常量取自契约。
  */
static void BL_DiagRoutineErase(const uint8_t *req, uint16_t len)
{
  uint8_t  resp[5];
  uint32_t size;
  bool     haveSize = false;

  if ((len != UDS_RID_ERASE_REQ_LEN_MIN) && (len != UDS_RID_ERASE_REQ_LEN_MAX))
  {
    BL_DiagSendNrc(UDS_SID_ROUTINE_CTRL, UDS_NRC_INCORRECT_MSG_LEN);
    return;
  }

  if (len == UDS_RID_ERASE_REQ_LEN_MAX)
  {
    size = (uint32_t)BL_DiagBe16(&req[4]);
    haveSize = true;
  }
  else
  {
    /* 不带 size：擦除整槽净荷区 */
    size = (uint32_t)OTA_IMG_MAX_PAYLOAD;
  }

  if ((size == 0U) || (size > (uint32_t)OTA_IMG_MAX_PAYLOAD))
  {
    resp[0] = 0x71U; resp[1] = 0x01U; resp[2] = 0xFFU; resp[3] = 0x00U; resp[4] = 0x01U;
    BL_DiagSend(resp, 5U);
    return;
  }

  {
    uint32_t appBase = ((1U - s_activeSlot) == 0U) ? (OTA_SLOT_A_ADDR + OTA_IMG_HDR_SIZE)
                                                   : (OTA_SLOT_B_ADDR + OTA_IMG_HDR_SIZE);
    resp[0] = 0x71U; resp[1] = 0x01U; resp[2] = 0xFFU; resp[3] = 0x00U;
    resp[4] = BL_DiagPrepareDownload(appBase, size) ? 0x00U : 0x01U;
  }
  (void)haveSize;
  BL_DiagSend(resp, 5U);
}

/**
  * @brief  FF01 校验：对本轮实际接收字节流重算 CRC32 并与参考值比对
  * @note   正响应 9 字节：[71,01,FF,01,status,crc32(4B 大端)]
  */
static void BL_DiagRoutineVerify(void)
{
  uint8_t  resp[BL_DIAG_VERIFY_RESP_LEN];
  uint32_t crcStream;
  uint32_t crcFlash;
  uint32_t refCrc   = 0U;
  bool     refValid = false;
  uint8_t  status;

  if (!s_dl.active)
  {
    BL_DiagSendNrc(UDS_SID_ROUTINE_CTRL, UDS_NRC_CONDITIONS);
    return;
  }

  /* 【M7 修复】0x34 会话参数合法性前置校验。
   * 原实现只检查 s_dl.active 就直接 BL_Crc32(s_dl.startAddr, s_dl.receivedLen)，
   * 于是两类异常路径会读到**语义错误但看似合法**的 CRC：
   *   - receivedLen == 0：CRC 恒为 0，任何"空"内容都算通过；
   *   - startAddr < appBase（或落在控制区/BL 区）：读的是别的镜像/别处数据，
   *     CRC 可能与上一轮镜像的 payloadCrc 恰好一致，于是给出"语义错误"的通过结论。
   * 这两类都必须在校验**之前**拦掉 —— 校验之后再看status 已无意义，
   * 因为 status 是"读完之后"的结论，而错误发生在"读"这个动作本身。 */
  if ((s_dl.receivedLen == 0U) || (s_dl.startAddr < s_dl.appBase))
  {
    resp[0] = 0x71U;
    resp[1] = 0x01U;
    resp[2] = 0xFFU;
    resp[3] = 0x01U;
    resp[4] = BL_DIAG_VERIFY_ST_LEN;                /* 会话参数非法：与"长度不符"同类 */
    resp[5] = 0U; resp[6] = 0U; resp[7] = 0U; resp[8] = 0U;
    BL_UART_SEND_STR("BL: FF01 bad session (len=0 or addr<appBase)\r\n");
    BL_DiagSend(resp, BL_DIAG_VERIFY_RESP_LEN);
    return;
  }

  crcStream = BL_Crc32Final(s_dl.runCrc);
  crcFlash  = BL_Crc32((const uint8_t *)s_dl.startAddr, s_dl.receivedLen);

  /* 参考值来源：0x37 携带值优先；其次槽首已有镜像头的 payloadCrc */
  if (s_dl.refCrcValid)
  {
    refCrc   = s_dl.refCrc;
    refValid = true;
  }
  else
  {
    BL_ImageInfo info;

    if ((BL_ImageReadHeader(s_dl.targetSlot, &info) == BL_IMG_OK) && (info.payloadSize == s_dl.receivedLen))
    {
      refCrc   = info.payloadCrc;
      refValid = true;
    }
  }

  if ((s_dl.receivedLen != s_dl.declaredSize) || (!s_dl.complete))
  {
    status = BL_DIAG_VERIFY_ST_LEN;               /* 长度不符 */
  }
  else if (crcStream != crcFlash)
  {
    status = BL_DIAG_VERIFY_ST_CRC;               /* 落盘内容与接收流不一致（写入异常） */
    BL_UART_SEND_STR("BL: FF01 flash/stream CRC mismatch\r\n");
  }
  else if (refValid && (crcStream != refCrc))
  {
    status = BL_DIAG_VERIFY_ST_CRC;
  }
  else
  {
    if (!refValid)
    {
      /* 无参考值（0x37 未带 CRC 且槽首无头）：仅长度校验，明确告警不静默 */
      BL_UART_SEND_STR("BL: FF01 no reference CRC, length-only\r\n");
    }
    status = BL_DIAG_VERIFY_ST_OK;
  }

  resp[0] = 0x71U;
  resp[1] = 0x01U;
  resp[2] = 0xFFU;
  resp[3] = 0x01U;
  resp[4] = status;
  resp[5] = (uint8_t)((crcStream >> 24) & 0xFFU);
  resp[6] = (uint8_t)((crcStream >> 16) & 0xFFU);
  resp[7] = (uint8_t)((crcStream >> 8) & 0xFFU);
  resp[8] = (uint8_t)(crcStream & 0xFFU);
  BL_DiagSend(resp, BL_DIAG_VERIFY_RESP_LEN);
}

/**
  * @brief  FF02 激活：合成槽首 32B 镜像头 -> 回读校验 -> 写配置 TRIAL_<目标槽>
  */
static void BL_DiagRoutineActivate(void)
{
  uint8_t       resp[5];
  uint8_t       hdr[OTA_IMG_HDR_SIZE];
  uint8_t       rc = 0x01U;
  OTA_Cfg       cfg;
  BL_ImageInfo  info;
  BL_ImgResult  vr;
  uint32_t      fullSize;
  uint32_t      crc;

  if ((!s_dl.active) || (!s_dl.complete))
  {
    BL_DiagSendNrc(UDS_SID_ROUTINE_CTRL, UDS_NRC_CONDITIONS);
    return;
  }
  if (s_dl.receivedLen != s_dl.declaredSize)
  {
    resp[0] = 0x71U; resp[1] = 0x01U; resp[2] = 0xFFU; resp[3] = 0x02U; resp[4] = 0x01U;
    BL_DiagSend(resp, 5U);
    return;
  }

  /* 续传时整镜像长度 = 已跳过的偏移 + 本次长度 */
  fullSize = (s_dl.startAddr - s_dl.appBase) + s_dl.receivedLen;

  crc = s_dl.refCrcValid ? s_dl.refCrc : BL_Crc32Final(s_dl.runCrc);
  if ((!s_dl.refCrcValid) && (s_dl.startAddr != s_dl.appBase))
  {
    /* 续传且 0x37 未带整镜像 CRC：无法还原整镜像 CRC，明确告警 */
    BL_UART_SEND_STR("BL: resume without ref CRC, header CRC may be wrong\r\n");
  }

  /* 设备侧合成 32B 镜像头（版本无来源，缺省 0） */
  BL_ImageBuildHeader(hdr, fullSize, s_dl.appBase, crc, 0U, 0U);

  if (BL_FlashWrite(s_dl.slotBase, hdr, OTA_IMG_HDR_SIZE) != OTA_FLASH_OK)
  {
    BL_UART_SEND_STR("BL: header write FAIL\r\n");
    BL_DiagRecordError(OTA_ERR_PROGRAM);
  }
  else if (BL_FlashVerify(s_dl.slotBase, hdr, OTA_IMG_HDR_SIZE) != OTA_FLASH_OK)
  {
    BL_UART_SEND_STR("BL: header readback FAIL\r\n");
    BL_DiagRecordError(OTA_ERR_PROGRAM);
  }
  else
  {
    /* 回读校验：把刚合成的头当作真实镜像**完整**校验一遍
     * （头 magic/hdrCrc + 净固件 CRC32 + 独立的 SP/复位向量检查）。
     * 只有全通过才写配置并回结果码 0x00，否则一律 0x01（Lead 的硬性要求）。 */
    vr = BL_ImageValidate(s_dl.targetSlot, &info);

    if (vr != BL_IMG_OK)
    {
      BL_UartPrintf("BL: synth header validate FAIL slot %u (err %u)\r\n",
                    (unsigned)s_dl.targetSlot, (unsigned)vr);
      BL_DiagRecordError(BL_ImageErrToOtaErr(vr));
    }
    else
    {
      /* 写配置：state = TRIAL_<目标槽>，trialLeft = 契约上限，CRC/长度来自本次校验 */
      cfg = *BL_ParamGet();
      cfg.state                        = BL_DiagTrialStateOf(s_dl.targetSlot);
      cfg.trialLeft                    = OTA_TRIAL_BOOT_LIMIT;
      cfg.slotCrc[s_dl.targetSlot]     = info.payloadCrc;
      cfg.slotSize[s_dl.targetSlot]    = info.payloadSize;
      cfg.slotVersion[s_dl.targetSlot] = info.version;
      cfg.lastError                    = OTA_ERR_NONE;

      if (BL_ParamStore(&cfg))
      {
        rc = 0x00U;
        BL_UartPrintf("BL: activated slot %u (size 0x%lX crc 0x%08lX)\r\n",
                      (unsigned)s_dl.targetSlot,
                      (unsigned long)info.payloadSize,
                      (unsigned long)info.payloadCrc);
      }
      else
      {
        BL_UART_SEND_STR("BL: cfg store FAIL\r\n");
      }
    }
  }

  resp[0] = 0x71U;
  resp[1] = 0x01U;
  resp[2] = 0xFFU;
  resp[3] = 0x02U;
  resp[4] = rc;
  BL_DiagSend(resp, 5U);
}

/**
  * @brief  FF03 回滚：切到另一槽并置 IDLE_<该槽>、trialLeft = 0
  */
static void BL_DiagRoutineRollback(void)
{
  uint8_t  resp[5];
  uint8_t  rc;
  uint8_t  other = (uint8_t)(1U - s_activeSlot);
  OTA_Cfg  cfg;
  BL_ImageInfo info;

  /* 只允许回滚到"确有完整可用镜像"的槽，避免主动切进 recovery 死循环 */
  if (BL_ImageValidate(other, &info) != BL_IMG_OK)
  {
    BL_UartPrintf("BL: rollback refused, slot %u image invalid\r\n", (unsigned)other);
    rc = 0x01U;
  }
  else
  {
    cfg = *BL_ParamGet();
    cfg.state     = BL_DiagIdleStateOf(other);
    cfg.trialLeft = 0U;
    cfg.lastError = OTA_ERR_NONE;
    rc = BL_ParamStore(&cfg) ? 0x00U : 0x01U;
    BL_UartPrintf("BL: rollback to slot %u rc=%u\r\n", (unsigned)other, (unsigned)rc);
  }

  resp[0] = 0x71U;
  resp[1] = 0x01U;
  resp[2] = 0xFFU;
  resp[3] = 0x03U;
  resp[4] = rc;
  BL_DiagSend(resp, 5U);
}

static void BL_DiagService31(const uint8_t *req, uint16_t len)
{
  uint16_t rid;

  if (!BL_DiagRequireExtended(UDS_SID_ROUTINE_CTRL))
  {
    return;
  }
  if (len < 4U)
  {
    BL_DiagSendNrc(UDS_SID_ROUTINE_CTRL, UDS_NRC_INCORRECT_MSG_LEN);
    return;
  }
  /* 【L2 修复】屏蔽 suppressPosRspMsg 位（bit7）。
   * ISO 14229-1：0x31 的子功能字节 bit7 是 suppressPosRspMsg 位，与 0x10/0x3E
   * 的处理保持一致（见BL_DiagService10 / BL_DiagServiceTesterPresent）。
   * 原实现直接 `req[1] != UDS_RC_START` 全字节比较，于是 `31 81 01 FF01`
   * （抑制肯定响应）被判为 SUBFUNC_NOT_SUP，回NRC 0x12 —— 与协议语义不符。
   * 正确做法：先屏蔽 bit7 再比较子功能号。
   * 注意 0x11（reset）之后不再检查该位，因为 reset 子功能不带此语义。 */
  if ((req[1] & UDS_SUBFUNC_MASK) != UDS_RC_START)
  {
    BL_DiagSendNrc(UDS_SID_ROUTINE_CTRL, UDS_NRC_SUBFUNC_NOT_SUP);
    return;
  }

  rid = BL_DiagBe16(&req[2]);
  switch (rid)
  {
    case UDS_RID_ERASE:
      BL_DiagRoutineErase(req, len);
      break;

    case UDS_RID_VERIFY:
      if (len != 4U)
      {
        BL_DiagSendNrc(UDS_SID_ROUTINE_CTRL, UDS_NRC_INCORRECT_MSG_LEN);
      }
      else
      {
        BL_DiagRoutineVerify();
      }
      break;

    case UDS_RID_ACTIVATE:
      if (len != 4U)
      {
        BL_DiagSendNrc(UDS_SID_ROUTINE_CTRL, UDS_NRC_INCORRECT_MSG_LEN);
      }
      else
      {
        BL_DiagRoutineActivate();
      }
      break;

    case UDS_RID_ROLLBACK:
      if (len != 4U)
      {
        BL_DiagSendNrc(UDS_SID_ROUTINE_CTRL, UDS_NRC_INCORRECT_MSG_LEN);
      }
      else
      {
        BL_DiagRoutineRollback();
      }
      break;

    case UDS_RID_COMMIT:
      /* FF04"试运行确认"（TRIAL_x -> IDLE_x）语义属于 APP：APP 自检通过后调用。
       * BL 不实现该例程，按 subFunctionNotSupported 回绝（Lead 已确认）。 */
      BL_DiagSendNrc(UDS_SID_ROUTINE_CTRL, UDS_NRC_SUBFUNC_NOT_SUP);
      break;

    default:
      BL_DiagSendNrc(UDS_SID_ROUTINE_CTRL, UDS_NRC_REQUEST_RANGE);
      break;
  }
}

/*--------------------------------------- 0x34 / 0x36 / 0x37 -----------------------------*/
static void BL_DiagService34(const uint8_t *req, uint16_t len)
{
  uint8_t  resp[4];
  uint32_t addr;
  uint32_t size;

  if (!BL_DiagRequireExtended(UDS_SID_REQUEST_DOWNLOAD))
  {
    return;
  }
  if (len != UDS_DL_REQ_LEN)
  {
    /* 34 00 44 + addr(4B 大端) + size(4B 大端) = 11 字节。
     * 曾误写为 10：而下面实际访问到 req[10]，自相矛盾，真机恒回 NRC 0x13。
     * 三方一律引用契约 UDS_DL_REQ_LEN，APP 侧 ota_agent.c 同源。 */
    BL_DiagSendNrc(UDS_SID_REQUEST_DOWNLOAD, UDS_NRC_INCORRECT_MSG_LEN);
    return;
  }
  if ((req[1] != 0x00U) || (req[2] != 0x44U))
  {
    /* 0x00 = 无压缩/加密；0x44 = 4 字节地址 + 4 字节长度 */
    BL_DiagSendNrc(UDS_SID_REQUEST_DOWNLOAD, UDS_NRC_REQUEST_RANGE);
    return;
  }

  addr = BL_DiagBe32(&req[3]);
  size = BL_DiagBe32(&req[7]);

  /* 放宽为"落在目标槽净荷区内、8 字节对齐、不越槽尾"，以支持断点续传 */
  if (!BL_DiagRangeOk(addr, size))
  {
    BL_DiagSendNrc(UDS_SID_REQUEST_DOWNLOAD, UDS_NRC_REQUEST_RANGE);
    return;
  }

  if (!BL_DiagPrepareDownload(addr, size))
  {
    BL_DiagSendNrc(UDS_SID_REQUEST_DOWNLOAD, UDS_NRC_UPLOAD_DOWNLOAD_NOK);
    return;
  }

  resp[0] = 0x74U;
  resp[1] = 0x20U;                                /* lengthFormatIdentifier：2 字节 maxLen */
  resp[2] = (uint8_t)((BL_DIAG_MAX_BLOCK_LEN >> 8) & 0xFFU);
  resp[3] = (uint8_t)(BL_DIAG_MAX_BLOCK_LEN & 0xFFU);
  BL_DiagSend(resp, 4U);
}

static void BL_DiagService36(const uint8_t *req, uint16_t len)
{
  uint8_t  resp[2];
  uint8_t  seq;
  uint8_t  expected;
  uint16_t n;

  if (!BL_DiagRequireExtended(UDS_SID_TRANSFER_DATA))
  {
    return;
  }
  if ((!s_dl.active) || s_dl.complete)
  {
    BL_DiagSendNrc(UDS_SID_TRANSFER_DATA, UDS_NRC_CONDITIONS);
    return;
  }
  if (len < 3U)
  {
    BL_DiagSendNrc(UDS_SID_TRANSFER_DATA, UDS_NRC_INCORRECT_MSG_LEN);
    return;
  }

  seq      = req[1];
  expected = BL_DiagExpectedSeq();                /* 1..255 循环，255 后回 1 */
  if ((seq == 0U) || (seq != expected))
  {
    /* 序号非法/不符：上位机按设备期望的序号重发该块 */
    BL_DiagSendNrc(UDS_SID_TRANSFER_DATA, UDS_NRC_WRONG_BLOCK_SN);
    return;
  }

  n = (uint16_t)(len - 2U);
  if ((s_dl.receivedLen + (uint32_t)n) > s_dl.declaredSize)
  {
    /* 超出 0x34 声明长度：回 NRC 0x70 并放弃本次下载 */
    s_dl.active = false;
    BL_DiagRecordError(OTA_ERR_SIZE_RANGE);
    BL_DiagSendNrc(UDS_SID_TRANSFER_DATA, UDS_NRC_UPLOAD_DOWNLOAD_NOK);
    return;
  }

  if (!BL_DiagAppendData(&req[2], (uint32_t)n))
  {
    BL_DiagSendNrc(UDS_SID_TRANSFER_DATA, UDS_NRC_UPLOAD_DOWNLOAD_NOK);
    return;
  }

  s_dl.blockCount++;

  resp[0] = 0x76U;
  resp[1] = seq;
  BL_DiagSend(resp, 2U);
}

static void BL_DiagService37(const uint8_t *req, uint16_t len)
{
  uint8_t resp[1];

  if (!BL_DiagRequireExtended(UDS_SID_TRANSFER_EXIT))
  {
    return;
  }
  if (!s_dl.active)
  {
    BL_DiagSendNrc(UDS_SID_TRANSFER_EXIT, UDS_NRC_CONDITIONS);
    return;
  }
  if ((len != 1U) && (len != 5U))
  {
    BL_DiagSendNrc(UDS_SID_TRANSFER_EXIT, UDS_NRC_INCORRECT_MSG_LEN);
    return;
  }

  if (len == 5U)
  {
    s_dl.refCrc      = BL_DiagBe32(&req[1]);      /* 整镜像净荷 CRC32（大端） */
    s_dl.refCrcValid = true;
  }
  else
  {
    BL_UART_SEND_STR("BL: 0x37 without CRC (length-only verify)\r\n");
  }

  /* 页缓冲残余字节落盘：BL_FlashWrite 内部按 8 字节 0xFF 补齐 */
  if (s_dl.pageFill > 0U)
  {
    if (BL_FlashWrite(s_dl.streamAddr, s_pageBuf, s_dl.pageFill) != OTA_FLASH_OK)
    {
      s_dl.active = false;
      BL_DiagRecordError(OTA_ERR_PROGRAM);
      BL_DiagSendNrc(UDS_SID_TRANSFER_EXIT, UDS_NRC_UPLOAD_DOWNLOAD_NOK);
      return;
    }
    s_dl.streamAddr += s_dl.pageFill;
    s_dl.pageFill    = 0U;
  }

  s_dl.complete = true;

  resp[0] = 0x77U;
  BL_DiagSend(resp, 1U);
}

/*--------------------------------------- 0x3E --------------------------------------------*/
static void BL_DiagService3E(const uint8_t *req, uint16_t len)
{
  uint8_t resp[2];

  if (len != 2U)
  {
    BL_DiagSendNrc(UDS_SID_TESTER_PRESENT, UDS_NRC_INCORRECT_MSG_LEN);
    return;
  }
  /* 屏蔽 bit7（suppressPosRspMsg）后比较子功能号 —— 用契约宏而非裸字面量 */
  if ((req[1] & UDS_SUBFUNC_MASK) != 0x00U)
  {
    BL_DiagSendNrc(UDS_SID_TESTER_PRESENT, UDS_NRC_SUBFUNC_NOT_SUP);
    return;
  }

  if ((req[1] & UDS_SUPPRESS_POS_RSP) != 0U)
  {
    return;                                     /* 3E 80：抑制正应答，无应答 */
  }

  resp[0] = 0x7EU;
  resp[1] = 0x00U;
  BL_DiagSend(resp, 2U);
}

/*--------------------------------------- 对外接口 ----------------------------------------*/
/**
  * @brief  初始化诊断状态机
  */
void BL_DiagInit(uint8_t activeSlot)
{
  (void)memset(&s_dl, 0, sizeof(s_dl));
  s_dl.runCrc   = BL_Crc32Init();
  s_activeSlot  = activeSlot;
  s_session     = BL_DIAG_SESSION_DEFAULT;
  s_lastActivity = HAL_GetTick();
  s_bootRequested = false;
  s_progErrors    = 0U;
}

/**
  * @brief  处理一条完整诊断请求
  */
void BL_DiagDispatch(const uint8_t *req, uint16_t len)
{
  if ((req == NULL) || (len < 1U) || (len > BL_ISOTP_MAX_MSG))
  {
    return;
  }

  s_lastActivity = HAL_GetTick();                 /* 任何有效请求刷新 S3 */

  switch (req[0])
  {
    case UDS_SID_DIAG_SESSION:    BL_DiagService10(req, len); break;
    case UDS_SID_ECU_RESET:       BL_DiagService11(req, len); break;
    case UDS_SID_READ_DID:        BL_DiagService22(req, len); break;
    case UDS_SID_WRITE_DID:       BL_DiagService2E(req, len); break;
    case UDS_SID_ROUTINE_CTRL:    BL_DiagService31(req, len); break;
    case UDS_SID_REQUEST_DOWNLOAD: BL_DiagService34(req, len); break;
    case UDS_SID_TRANSFER_DATA:   BL_DiagService36(req, len); break;
    case UDS_SID_TRANSFER_EXIT:   BL_DiagService37(req, len); break;
    case UDS_SID_TESTER_PRESENT:  BL_DiagService3E(req, len); break;

    default:
      BL_DiagSendNrc(req[0], UDS_NRC_SERVICE_NOT_SUP);
      break;
  }
}

/**
  * @brief  S3 会话超时：扩展会话 5s 无请求回默认会话
  */
void BL_DiagTask(void)
{
  if ((s_session == BL_DIAG_SESSION_EXTENDED) &&
      ((HAL_GetTick() - s_lastActivity) > BL_DIAG_S3_TIMEOUT_MS))
  {
    s_session = BL_DIAG_SESSION_DEFAULT;
  }
}

/**
  * @brief  0x2E F190 是否收到过
  */
bool BL_DiagBootRequested(void)
{
  return s_bootRequested;
}

/**
  * @brief  下载事务是否进行中
  */
bool BL_DiagDownloadActive(void)
{
  return s_dl.active;
}
