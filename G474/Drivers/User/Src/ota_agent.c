/**
  ******************************************************************************
  * @file    ota_agent.c
  * @brief   APP 侧 OTA 代理实现（0x34/0x36/0x37/0x31/0x11 + F180~F190 DID）
  *
  * 关键设计（逐条对应设计文档 03/04）：
  *   1. 目标槽 = 非活动槽（ActiveSlot ^ 1），正在运行的固件绝不被写入；
  *   2. 2 KB 页缓冲：0x36 先攒进 RAM，满页才擦写一次，避免同页反复擦除；
  *   3. 边收边算 CRC32，FF01 例程据此判定完整性；
  *   4. 试运行（TRIAL）固件在自检通过前不落定，超时则由 Bootloader 自动回滚；
  *   5. 所有擦写地址经过 OTA 区间的硬保护，Bootloader 区（< OTA_SLOT_A_ADDR）不可触碰。
  ******************************************************************************
  */
#include "ota_agent.h"
#include "can.h"
#include "isotp.h"
#include "bsp_uart.h"
#include <string.h>

/* main.c 中的看门狗句柄（页编程前喂狗，避免长操作触发 3s 复位） */
extern IWDG_HandleTypeDef hiwdg;

/*--------------------------------------- 内部常量 --------------------------------------*/
/* 单块净荷上限不再本地定义：统一取契约 ota_common.h 的 UDS_MAXLEN_FIELD
 * （= OTA_ISOTP_MAX_MSG - 3），避免"契约改了、实现没跟"的静默不一致。 */

/*--------------------------------------- 内部状态 --------------------------------------*/
typedef enum
{
  OTA_DL_IDLE = 0,        /* 无下载事务 */
  OTA_DL_ACTIVE,          /* 0x34 已受理，正在收 0x36 */
  OTA_DL_COMPLETE,        /* 0x37 已收尾，等待 FF01 校验 / FF02 激活 */
} Ota_DlState;

static OTA_Cfg  s_cfg;                                   /* 控制区当前值 */
static uint8_t  s_runningSlot;                           /* 本固件所在槽：0=A 1=B */
static bool     s_trialPending;                          /* 试运行待确认 */
static uint32_t s_selfCheckTick;

static Ota_DlState     s_dl;                             /* 下载状态机 */
static uint8_t         s_targetSlot;                     /* 本次下载目标槽 */
static uint32_t        s_targetAddr;                     /* 目标写入地址（随页推进） */
/**
 * @brief  FF02 激活失败原因码（放进 Ota_RoutineResult 的 result 字节）
 * @note   诊断投送渠道问题：目标板串口不一定能抓到（抓串口本身会复位它），
 *         故把失败细分码随 0x31 应答回传给上位机 —— 这是唯一可靠的观测通道。
 *         0x01 保留为"未知/未细分"。
 */
#define OTA_ACT_OK            0x00U
#define OTA_ACT_ERR_PRECHECK          0xA0U   /* s_dl != COMPLETE 或 received != declared */
#define OTA_ACT_ERR_FLASH          0xA1U   /* 头写入失败（HAL_FLASH_Program 报错） */
#define OTA_ACT_ERR_MAGIC          0xA2U   /* 回读 magic 不符 */
#define OTA_ACT_ERR_PSIZE          0xA3U   /* 回读 payloadSize 不符 */
#define OTA_ACT_ERR_LOADADDR          0xA4U   /* 回读 loadAddr 不符 */
#define OTA_ACT_ERR_HDRCRC          0xA5U   /* 回读 hdrCrc 与实算不符 */
#define OTA_ACT_ERR_CFGWRITE          0xA6U   /* 控制区页写失败（TRIAL 标志落盘失败） */

static uint8_t         s_actErr = 0xEEU;                  /* FF02 激活失败细分码（随 0x31 应答回传）；0xEE = "未被赋值"哨兵 */
static uint32_t        s_declaredSize;                   /* 0x34 声明的净固件长度 */
static uint32_t        s_received;                       /* 已接收字节数 */
static uint32_t        s_runningCrc;                     /* 已接收数据的 CRC32 */
static uint8_t         s_expectedSeq;                    /* 下一个期望的块序号（1..255 循环） */
static uint8_t         s_pageBuf[OTA_PAGE_BUF_SIZE];     /* 页缓冲（2 KB） */
static uint32_t        s_pageFill;                       /* 页缓冲已填充字节数 */

/* 0x37 若携带 CRC 则记录在此，FF01 时优先使用 */
static bool            s_exitCrcValid;
static uint32_t        s_exitCrc;
static bool            s_bootReqPending;                 /* 0x2E F190 置位 */

/*--------------------------------------- 小工具 ----------------------------------------*/
static uint16_t U16BE(const uint8_t *p) { return (uint16_t)(((uint16_t)p[0] << 8) | p[1]); }
static uint32_t U32BE(const uint8_t *p)
{
  return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) | ((uint32_t)p[2] << 8) | p[3];
}
static void PutU32BE(uint8_t *p, uint32_t v)
{
  p[0] = (uint8_t)(v >> 24); p[1] = (uint8_t)(v >> 16);
  p[2] = (uint8_t)(v >> 8);  p[3] = (uint8_t)v;
}
static uint32_t Ota_SlotBase(uint8_t slot)
{
  return (slot == 0U) ? OTA_SLOT_A_ADDR : OTA_SLOT_B_ADDR;
}
static uint32_t Ota_AppBase(uint8_t slot)   /* APP 链接基址 = 槽基址 + 镜像头 */
{
  return Ota_SlotBase(slot) + OTA_IMG_HDR_SIZE;
}
/* 镜像头字段全为小端（ota_common.h §149）；BL 侧的 BL_ImageRdU32/BL_ImageWrU32
 * 属 Bootloader 工程，APP 侧不可引用，故本地各备一份。 */
static void PutU32LE(uint8_t *p, uint32_t v)
{
  p[0] = (uint8_t)v;         p[1] = (uint8_t)(v >> 8);
  p[2] = (uint8_t)(v >> 16); p[3] = (uint8_t)(v >> 24);
}
static uint32_t RdU32LE(const uint8_t *p)
{
  return (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
         ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}
static uint8_t Ota_ActiveSlotOf(uint32_t state)
{
  return (uint8_t)((state == OTA_ST_IDLE_B || state == OTA_ST_TRIAL_B) ? 1U : 0U);
}

/*--------------------------------------- 应答发送 --------------------------------------*/
static void Ota_Send(const uint8_t *resp, uint16_t len)
{
  (void)IsoTp_Send(resp, len);              /* 忙则丢弃：上位机会因超时重试 */
}
static void Ota_SendNrc(uint8_t sid, uint8_t nrc)
{
  uint8_t r[3];
  r[0] = 0x7FU; r[1] = sid; r[2] = nrc;
  Ota_Send(r, 3U);
}

/*--------------------------------------- CRC32 ==========================================*/
/**
  * @brief  标准 CRC-32/ISO-HDLC（与 zlib.crc32 一致），支持分片累加
  * @param  crc 上一片的返回值；首片传 0xFFFFFFFF
  */
static uint32_t Ota_Crc32Update(uint32_t crc, const uint8_t *data, uint32_t len)
{
  for (uint32_t i = 0U; i < len; i++)
  {
    crc ^= data[i];
    for (uint8_t b = 0U; b < 8U; b++)
    {
      crc = ((crc & 1U) != 0U) ? ((crc >> 1) ^ 0xEDB88320U) : (crc >> 1);
    }
  }
  return crc;
}
static uint32_t Ota_Crc32(const uint8_t *data, uint32_t len)
{
  return ~Ota_Crc32Update(0xFFFFFFFFU, data, len);
}

/*--------------------------------------- Flash 访问（带保护区） -------------------------*/
typedef enum
{
  OTA_FL_OK = 0, OTA_FL_ERR_PROTECT, OTA_FL_ERR_ERASE, OTA_FL_ERR_PROGRAM, OTA_FL_ERR_RANGE
} Ota_FlashResult;

/**
  * @brief  绝对地址 -> HAL 页擦除所需的 (Bank, Bank 内页号)
  * @note   HAL 的 FLASH_PageErase() 把页号直接写入 FLASH_CR.PNB（8 位，0~255）：
  *           - DBANK=1（双 Bank）：PNB 是**Bank 内**页号（0~127），Bank 由 FLASH_CR.BKSEL 选；
  *           - DBANK=0（单 Bank）：PNB 是 0~255 的绝对页号。
  *         因此双 Bank 下必须扣掉 256KB 偏移再除以页大小，否则 PNB 写入 128~255 会越界/擦错页。
  *         这里与 Bootloader 的 BL_FlashPageOf() 保持同一语义。
  */
static uint32_t Ota_FlashPageOf(uint32_t addr, uint32_t *bank)
{
  uint32_t offset = addr - OTA_FLASH_BASE;

  *bank = FLASH_BANK_1;
#if defined(FLASH_OPTR_DBANK)
  if ((FLASH->OPTR & FLASH_OPTR_DBANK) != 0U)      /* 出厂 512KB 双 Bank 配置 */
  {
    if (offset >= OTA_FLASH_BANK_SIZE)             /* 本槽位于 Bank2 */
    {
      *bank = FLASH_BANK_2;
      offset -= OTA_FLASH_BANK_SIZE;               /* 256KB -> PNB 回到 0..127 */
    }
  }
#endif
  return offset / OTA_FLASH_PAGE_SIZE;
}

/**
  * @brief  擦除 [addr, addr+size) 覆盖的全部 2KB 页
  * @note   任何落在 Bootloader 区（< OTA_SLOT_A_ADDR）的请求一律拒绝
  */
static Ota_FlashResult Ota_FlashEraseRange(uint32_t addr, uint32_t size)
{
  FLASH_EraseInitTypeDef erase;
  uint32_t               pageError = 0U;
  uint32_t               bank;
  uint32_t               npages;

  if ((size == 0U) || ((addr % OTA_FLASH_PAGE_SIZE) != 0U))
  {
    return OTA_FL_ERR_RANGE;
  }
  if (addr < OTA_SLOT_A_ADDR)                       /* ★ 硬保护：Bootloader 区不可擦 */
  {
    return OTA_FL_ERR_PROTECT;
  }
  npages = (size + OTA_FLASH_PAGE_SIZE - 1U) / OTA_FLASH_PAGE_SIZE;
  if ((addr + (npages * OTA_FLASH_PAGE_SIZE)) > (OTA_FLASH_BASE + OTA_FLASH_SIZE))
  {
    return OTA_FL_ERR_RANGE;
  }

  if (HAL_FLASH_Unlock() != HAL_OK)
  {
    return OTA_FL_ERR_ERASE;
  }

  for (uint32_t i = 0U; i < npages; i++)
  {
    HAL_IWDG_Refresh(&hiwdg);                       /* ★ 每页擦除前喂狗 */
    /* 逐页擦：G4 双 Bank 下单次擦除请求不能跨 Bank 边界 */
    erase.TypeErase = FLASH_TYPEERASE_PAGES;
    erase.Page      = Ota_FlashPageOf(addr + (i * OTA_FLASH_PAGE_SIZE), &bank);
    erase.Banks     = bank;                         /* 双 Bank 时 HAL 据此置 BKSEL */
    erase.NbPages   = 1U;
    pageError       = 0U;

    if (HAL_FLASHEx_Erase(&erase, &pageError) != HAL_OK)
    {
      (void)HAL_FLASH_Lock();
      return OTA_FL_ERR_ERASE;
    }
  }

  (void)HAL_FLASH_Lock();
  return OTA_FL_OK;
}

/**
  * @brief  按 64 位双字写入（Flash 最小编程粒度），长度自动补齐到 8 的倍数
  */
static Ota_FlashResult Ota_FlashWrite(uint32_t addr, const uint8_t *data, uint32_t len)
{
  uint32_t a = addr;
  uint32_t i = 0U;

  if ((data == NULL) || (len == 0U) || ((addr % 8U) != 0U))
  {
    return OTA_FL_ERR_RANGE;
  }
  if (addr < OTA_SLOT_A_ADDR)                       /* ★ 硬保护 */
  {
    return OTA_FL_ERR_PROTECT;
  }
  if ((addr + len) > (OTA_FLASH_BASE + OTA_FLASH_SIZE))
  {
    return OTA_FL_ERR_RANGE;
  }

  if (HAL_FLASH_Unlock() != HAL_OK)
  {
    return OTA_FL_ERR_PROGRAM;
  }

  while (i < len)
  {
    uint64_t dw = 0U;
    for (uint8_t b = 0U; b < 8U; b++)
    {
      uint8_t byte = ((i + b) < len) ? data[i + b] : 0xFFU;   /* 尾部 0xFF 补齐，保持 Flash 空态 */
      dw |= ((uint64_t)byte << (8U * b));
    }
    if (HAL_FLASH_Program(FLASH_TYPEPROGRAM_DOUBLEWORD, a, dw) != HAL_OK)
    {
      (void)HAL_FLASH_Lock();
      return OTA_FL_ERR_PROGRAM;
    }
    a += 8U;
    i += 8U;
  }

  (void)HAL_FLASH_Lock();
  return OTA_FL_OK;
}

/*--------------------------------------- 控制区读写 -------------------------------------*/
static bool Ota_CfgValid(const OTA_Cfg *c)
{
  if (c->magic != OTA_CFG_MAGIC)
  {
    return false;
  }
  return (Ota_Crc32((const uint8_t *)c, (uint32_t)offsetof(OTA_Cfg, crc)) == c->crc);
}

/**
  * @brief  读取控制区：4 个候选取"有效且 seq 最大"者
  * @retval true=读到有效配置；false=全部无效（首次上电/被擦），s_cfg 被置默认值
  */
static bool Ota_ParamLoad(void)
{
  const uint32_t cand[4] =
  {
    OTA_CFG_PRIMARY_ADDR + OTA_CFG_SLOT_OFFSET,
    OTA_CFG_PRIMARY_ADDR + OTA_CFG_SLOT_MIRROR_OFF,
    OTA_CFG_MIRROR_ADDR  + OTA_CFG_SLOT_OFFSET,
    OTA_CFG_MIRROR_ADDR  + OTA_CFG_SLOT_MIRROR_OFF,
  };
  const OTA_Cfg *best = NULL;
  OTA_Cfg        tmp;

  for (uint8_t i = 0U; i < 4U; i++)
  {
    (void)memcpy(&tmp, (const void *)cand[i], sizeof(tmp));
    if (Ota_CfgValid(&tmp))
    {
      if ((best == NULL) || (tmp.seq > best->seq))
      {
        best = (const OTA_Cfg *)cand[i];
      }
    }
  }

  if (best == NULL)
  {
    (void)memset(&s_cfg, 0, sizeof(s_cfg));         /* 默认：IDLE_A，两槽均视为无效 */
    s_cfg.magic  = OTA_CFG_MAGIC;
    s_cfg.seq    = 1U;
    s_cfg.state  = OTA_ST_IDLE_A;
    s_cfg.crc    = Ota_Crc32((const uint8_t *)&s_cfg, (uint32_t)offsetof(OTA_Cfg, crc));
    return false;
  }

  (void)memcpy(&s_cfg, best, sizeof(s_cfg));
  return true;
}

/**
  * @brief  本地落定试运行固件（TRIAL_x -> IDLE_x）
  * @note   与 Bootloader 的写策略一致：先整页暂存 -> 擦页 -> 回写"另一份" -> 写新值。
  *         本函数只在自检通过后调用一次，调用时系统已稳定，掉电窗口极小。
  */
/**
  * @brief  写一个控制区页：整页暂存 -> 擦页 -> 整页回写（含另一份副本）
  * @note   控制区页位于 0x08000000 / 0x08040000，**低于 OTA_SLOT_A_ADDR**，
  *         因此不能走 Ota_FlashEraseRange 的"< Slot A 一律拒绝"保护 —— 那是给
  *         APP 下载流程用的保护，而控制区是它自己的数据。这里使用显式白名单：
  *         仅允许这两个控制区页基址，其余地址一律拒绝。
  */
static bool Ota_CfgBlockWrite(uint32_t pageBase, uint32_t dstOff, const OTA_Cfg *nv)
{
  FLASH_EraseInitTypeDef erase;
  uint32_t               pageError = 0U;
  uint32_t               bank;

  if ((pageBase != OTA_CFG_PRIMARY_ADDR) && (pageBase != OTA_CFG_MIRROR_ADDR))
  {
    return false;                                   /* 白名单之外一律不动 Flash */
  }
  if (((dstOff + sizeof(OTA_Cfg)) > OTA_CFG_PAGE_SIZE) ||
      ((dstOff % 8U) != 0U))
  {
    return false;
  }

  /* 1. 整页暂存（含页首 BL 向量表与页内另一份副本，擦后原样回写） */
  (void)memcpy(s_pageBuf, (const void *)pageBase, OTA_CFG_PAGE_SIZE);
  (void)memset(&s_pageBuf[dstOff], 0xFF, sizeof(OTA_Cfg));   /* 先擦态，保证尾部补齐字节为 0xFF */
  (void)memcpy(&s_pageBuf[dstOff], nv, sizeof(OTA_Cfg));
  /* sizeof(OTA_Cfg) = 48（8 的倍数，此循环为 0 次）；结构若增长到非 8 倍数，
   * 本循环自动把尾部补齐为 0xFF，保证双字编程不会把残余的上一份数据写回去 */
  for (uint8_t b = (uint8_t)sizeof(OTA_Cfg); b < 8U; b++)
  {
    s_pageBuf[dstOff + b] = 0xFFU;
  }

  /* 2. 擦页（地址必须页对齐） */
  if (HAL_FLASH_Unlock() != HAL_OK)
  {
    return false;
  }
  HAL_IWDG_Refresh(&hiwdg);
  erase.TypeErase = FLASH_TYPEERASE_PAGES;
  erase.Page      = Ota_FlashPageOf(pageBase, &bank);   /* Bank 内页号；控制页多为 0 */
  erase.Banks     = bank;                               /* 镜像份页(0x08040000)属于 Bank2 */
  erase.NbPages   = 1U;
  if (HAL_FLASHEx_Erase(&erase, &pageError) != HAL_OK)
  {
    (void)HAL_FLASH_Lock();
    return false;
  }

  /* 3. 整页回写 */
  for (uint32_t a = pageBase; a < (pageBase + OTA_CFG_PAGE_SIZE); a += 8U)
  {
    uint64_t dw = 0U;
    uint32_t i  = a - pageBase;
    for (uint8_t b = 0U; b < 8U; b++)
    {
      dw |= ((uint64_t)s_pageBuf[i + b] << (8U * b));
    }
    if (HAL_FLASH_Program(FLASH_TYPEPROGRAM_DOUBLEWORD, a, dw) != HAL_OK)
    {
      (void)HAL_FLASH_Lock();
      return false;
    }
  }
  (void)HAL_FLASH_Lock();

  /* 4. 回读校验 */
  {
    OTA_Cfg chk;
    (void)memcpy(&chk, (const void *)(pageBase + dstOff), sizeof(chk));
    return Ota_CfgValid(&chk) && (chk.seq == nv->seq);
  }
}

static bool Ota_ParamCommitTrial(void)
{
  uint32_t pageBase, dstOff;
  OTA_Cfg  nv;

  if (!s_trialPending)
  {
    return true;                                    /* 非试运行，无需落定 */
  }

  /* 选择页：优先用与本槽不同的那份控制区，避免更新 Slot B 时刚被擦过的那页 */
  pageBase = (s_runningSlot == 1U) ? OTA_CFG_PRIMARY_ADDR : OTA_CFG_MIRROR_ADDR;
  dstOff   = OTA_CFG_SLOT_MIRROR_OFF;

  {
    OTA_Cfg p0, p1;
    bool    v0, v1;
    (void)memcpy(&p0, (const void *)(pageBase + OTA_CFG_SLOT_OFFSET),     sizeof(p0));
    (void)memcpy(&p1, (const void *)(pageBase + OTA_CFG_SLOT_MIRROR_OFF), sizeof(p1));
    v0 = Ota_CfgValid(&p0);
    v1 = Ota_CfgValid(&p1);

    /* 目标偏移 = 更旧/无效的那一份；另一份保持不动 */
    if (v0 && v1)
    {
      bool dstIs0 = (p0.seq <= p1.seq);
      dstOff   = dstIs0 ? OTA_CFG_SLOT_OFFSET : OTA_CFG_SLOT_MIRROR_OFF;
    }
    else if (v0)
    {
      dstOff   = OTA_CFG_SLOT_MIRROR_OFF;
    }
    else
    {
      dstOff   = OTA_CFG_SLOT_OFFSET;
    }
  }

  nv       = s_cfg;
  nv.state = (s_runningSlot == 0U) ? OTA_ST_IDLE_A : OTA_ST_IDLE_B;
  nv.trialLeft = 0U;
  nv.seq++;
  nv.crc   = Ota_Crc32((const uint8_t *)&nv, (uint32_t)offsetof(OTA_Cfg, crc));

  if (Ota_CfgBlockWrite(pageBase, dstOff, &nv))
  {
    s_cfg          = nv;
    s_trialPending = false;
    BSP_UART_Send("ota: trial committed (IDLE)\r\n", 30U);
    return true;
  }

  BSP_UART_Send("ota: commit FAILED (will rollback)\r\n", 36U);
  return false;
}

/*--------------------------------------- 页缓冲与落盘 -----------------------------------*/
static void Ota_PageFlush(bool final)
{
  uint32_t writeLen;

  if ((s_pageFill == 0U) && !final)
  {
    return;
  }

  writeLen = final ? ((s_pageFill + 7U) & ~7U) : OTA_PAGE_BUF_SIZE;  /* 收尾补齐到 8 字节 */
  if (writeLen == 0U)
  {
    s_pageFill = 0U;
    return;
  }

  HAL_IWDG_Refresh(&hiwdg);                         /* ★ 页编程前喂狗 */

  if (Ota_FlashWrite(s_targetAddr, s_pageBuf, writeLen) != OTA_FL_OK)
  {
    s_cfg.lastError = OTA_ERR_PROGRAM;
    BSP_UART_Send("ota: flash write FAIL\r\n", 23U);
    s_dl = OTA_DL_IDLE;                             /* 放弃本次下载 */
    return;
  }

  s_targetAddr += writeLen;
  s_pageFill    = 0U;
}

/*--------------------------------------- 0x34 RequestDownload ---------------------------*/
/* ★ 诊断细分码：0x34 的 NRC 0x31 有四条出处（下方503/512/527/534），
 *   单看 NRC 无法区分是addr 区间错还是 size 越界。而抓目标板串口会复位它，
 *   故把细分码编码进 **0x78 responsePending 的 rlen**：不可为0 的 rlen 在
 *   ISO-TP 里天然表达"还有后续帧"，对端收到后会继续等 —— 正好用来携带诊断信息，
 *   且不影响正常应答流程（客户端本来就忽略 0x78）。
 *   细分码见 OTA_DL_DIAG_* 宏。 */
#define OTA_DL_DIAG_DFID      0xD1U   /* 501: dataIdentifier 格式不支持 */
#define OTA_DL_DIAG_SIZE      0xD2U   /* 510: size 为 0 或超上限 */
#define OTA_DL_DIAG_SLOT      0xD3U   /* 527: addr 不落在任何槽 */
#define OTA_DL_DIAG_OVERRUN   0xD4U   /* 532: addr+size 越过槽尾 */
#define OTA_DL_DIAG_ACTIVE    0xD5U   /* 539: 目标是活动槽 */
#define OTA_DL_DIAG_LEN       0xD6U   /* 496: 请求长度非 11 */

/* 最近一次 0x34 的 addr/size（供 Ota_DlDiag 打印，必须先于该函数定义） */
static uint32_t s_dlDiagAddr;
static uint32_t s_dlDiagSize;

/** 回一个带诊断细分码的 0x78（rlen = 细分码，客户端会忽略但串口日志可见） */
static void Ota_DlDiag(uint8_t code)
{
  /* ★【2026-10-18】这里原先会额外发一帧 7F 34 <code> 到 CAN，仅供取证。
   *   一旦客户端的 NRC 解析被修好，这帧就会被当成真正的负响应而中断流程
   *   （实测：0x34 内容已完全正确，却因这条诊断帧被判 NRC 0xE0 失败）。
   *   UDS 也不允许一个请求回两条负响应，故改为**只打本端串口**，
   *   既保留"哪条判据没过"的现场信息，又不污染诊断协议。 */
  BSP_UART_Printf("ota: 0x34 REJECT code=0x%02X addr=0x%08lX size=%lu\r\n",
                  code, (unsigned long)s_dlDiagAddr, (unsigned long)s_dlDiagSize);
}

void Ota_OnRequestDownload(const uint8_t *req, uint16_t len)
{
  uint32_t addr, size;
  uint8_t  wantSlot;

  /* 【2026-10-18】原先这里会把 0x34 的原始内容打包成 7F 34 E0 通过 CAN 回传，
   * 仅用于"板 B 到底看到了什么"的取证。多帧链路修好之后它反而成了障碍：
   *   客户端已能正确解析负响应，这条诊断帧会被判为致命 NRC（实测 7F 34 E0）
   *   而中断整个 OTA；UDS 也不允许一个请求回两条负响应。
   * 取证目的已达成（addr/size 均确认正确），改为**只打本端串口**。 */
  if ((req != NULL) && (len >= 7U))
  {
    BSP_UART_Printf("ota: 0x34 RAW len=%u addr=0x%08lX size=0x%08lX raw0=%02X\r\n",
                    (unsigned)len, (unsigned long)U32BE(&req[3]),
                    (unsigned long)U32BE(&req[7]), req[0]);
  }

  if (len != UDS_DL_REQ_LEN)                         /* 34 00 44 + 4B addr + 4B size = 11 */
  {
    Ota_DlDiag(OTA_DL_DIAG_LEN);
    Ota_SendNrc(UDS_SID_REQUEST_DOWNLOAD, UDS_NRC_INCORRECT_MSG_LEN);
    return;
  }
  addr = U32BE(&req[3]);
  size = U32BE(&req[7]);
  s_dlDiagAddr = addr;
  s_dlDiagSize = size;

  if ((req[1] != 0x00U) || (req[2] != 0x44U))       /* 仅支持 4 字节地址 + 4 字节长度 */
  {
    Ota_DlDiag(OTA_DL_DIAG_DFID);
    Ota_SendNrc(UDS_SID_REQUEST_DOWNLOAD, UDS_NRC_REQUEST_RANGE);
    return;
  }

  if ((size == 0U) || (size > OTA_IMG_MAX_PAYLOAD))
  {
    Ota_DlDiag(OTA_DL_DIAG_SIZE);
    Ota_SendNrc(UDS_SID_REQUEST_DOWNLOAD, UDS_NRC_REQUEST_RANGE);
    return;
  }

  /* 目标槽判定 + 允许断点续传（addr 落在槽内任意位置，size 不越槽尾） */
  if ((addr >= Ota_AppBase(0U)) && (addr < (Ota_AppBase(0U) + OTA_IMG_MAX_PAYLOAD)))
  {
    wantSlot = 0U;
  }
  else if ((addr >= Ota_AppBase(1U)) && (addr < (Ota_AppBase(1U) + OTA_IMG_MAX_PAYLOAD)))
  {
    wantSlot = 1U;
  }
  else
  {
    Ota_DlDiag(OTA_DL_DIAG_SLOT);
    Ota_SendNrc(UDS_SID_REQUEST_DOWNLOAD, UDS_NRC_REQUEST_RANGE);
    return;
  }

  /* size 不得越过该槽上界 */
  if ((addr + size) > (Ota_AppBase(wantSlot) + OTA_IMG_MAX_PAYLOAD))
  {
    Ota_DlDiag(OTA_DL_DIAG_OVERRUN);
    Ota_SendNrc(UDS_SID_REQUEST_DOWNLOAD, UDS_NRC_REQUEST_RANGE);
    return;
  }

  /* ★ 核心安全约束：只允许写"非活动槽"，正在运行的本槽永不作为目标 */
  if (wantSlot == s_runningSlot)
  {
    Ota_DlDiag(OTA_DL_DIAG_ACTIVE);
    Ota_SendNrc(UDS_SID_REQUEST_DOWNLOAD, UDS_NRC_CONDITIONS);
    return;
  }

  s_targetSlot   = wantSlot;
  s_declaredSize = size;
  s_received     = 0U;
  s_runningCrc   = 0xFFFFFFFFU;
  s_pageFill     = 0U;
  s_expectedSeq  = 1U;
  s_exitCrcValid = false;
  s_targetAddr   = addr;                            /* 续传时 = 槽 APP 基址 + 已传长度 */

  /* 擦除 [addr, addr+size) 覆盖的页。
   * ★【2026-10-12 修复】0x34 的 addr 语义是"APP 链接基址 = 槽基址 + OTA_IMG_HDR_SIZE"，
   *   而 0x34 协议只允许 4 字节地址，无法表达"从槽首开始擦、跳过已存在镜像头"。
   *   而 OTA_IMG_HDR_SIZE = 0x80 在 2KB 页内，故 slotBase+0x80 **天然不是页对齐**
   *   （0x08040880 % 0x800 == 0x80），直接传给要求页对齐的擦除函数会返回
   *   ERR_RANGE → 对端收到 NRC 0x34/0x70（uploadDownloadNotAccepted）。
   *   之前没暴露是因为 Slot B 长期为空、且早期 addr 传的是槽基址（恰好对齐）。
   *   处置：向下对齐到页首，擦除范围相应扩大 —— 头部所在页本来就要擦（重写头），
   *   扩大擦除是正确且必要的。 */
  {
    uint32_t eraseAddr = s_targetAddr & ~(OTA_FLASH_PAGE_SIZE - 1U);
    uint32_t eraseSize = (s_targetAddr - eraseAddr) + size;

    /* 按 UDS 规范先回 0x78 responsePending：整槽擦除约 1s 以上，
     * 若上位机应答窗口 < 1s 会误判超时并重试，撞上仍在进行的擦除。 */
    {
      uint8_t pend[3];
      pend[0] = 0x7FU;
      pend[1] = UDS_SID_REQUEST_DOWNLOAD;
      pend[2] = 0x78U;                            /* responsePending */
      Ota_Send(pend, 3U);
    }

    if (Ota_FlashEraseRange(eraseAddr, eraseSize) != OTA_FL_OK)
    {
      s_cfg.lastError = OTA_ERR_ERASE;
      BSP_UART_Printf("ota: 0x34 erase FAIL addr=0x%08lX size=%lu\r\n",
                      (unsigned long)eraseAddr, (unsigned long)eraseSize);
      Ota_SendNrc(UDS_SID_REQUEST_DOWNLOAD, UDS_NRC_UPLOAD_DOWNLOAD_NOK);
      return;
    }
  }

  s_dl = OTA_DL_ACTIVE;

  {
    uint8_t resp[4];
    resp[0] = 0x74U;
    resp[1] = 0x20U;                                /* lengthFormatIdentifier：2 字节 maxLen */
    resp[2] = (uint8_t)(UDS_MAXLEN_FIELD >> 8);
    resp[3] = (uint8_t)(UDS_MAXLEN_FIELD & 0xFFU);
    Ota_Send(resp, 4U);
  }
}

/*--------------------------------------- 0x36 TransferData -----------------------------*/
void Ota_OnTransferData(const uint8_t *req, uint16_t len)
{
  uint8_t  seq;
  uint16_t n;

  if (len < 3U)
  {
    Ota_SendNrc(UDS_SID_TRANSFER_DATA, UDS_NRC_INCORRECT_MSG_LEN);
    return;
  }
  if (s_dl != OTA_DL_ACTIVE)
  {
    Ota_SendNrc(UDS_SID_TRANSFER_DATA, UDS_NRC_CONDITIONS);
    return;
  }

  seq = req[1];
  if (seq == 0U)                                    /* ★ 块序号 1..255 循环，0 非法 */
  {
    Ota_SendNrc(UDS_SID_TRANSFER_DATA, UDS_NRC_WRONG_BLOCK_SN);
    return;
  }
  if (seq != s_expectedSeq)
  {
    Ota_SendNrc(UDS_SID_TRANSFER_DATA, UDS_NRC_WRONG_BLOCK_SN);
    return;
  }

  n = (uint16_t)(len - 2U);
  if ((s_received + (uint32_t)n) > s_declaredSize)
  {
    s_cfg.lastError = OTA_ERR_SIZE_RANGE;
    s_dl = OTA_DL_IDLE;
    Ota_SendNrc(UDS_SID_TRANSFER_DATA, UDS_NRC_UPLOAD_DOWNLOAD_NOK);
    return;
  }

  /* 先算 CRC（数据一定合法），再按页缓冲落盘 */
  s_runningCrc = Ota_Crc32Update(s_runningCrc, &req[2], (uint32_t)n);
  s_received  += (uint32_t)n;

  {
    uint16_t off = 0U;
    while (off < n)
    {
      uint32_t space = OTA_PAGE_BUF_SIZE - s_pageFill;
      uint32_t chunk = ((uint32_t)(n - off) < space) ? (uint32_t)(n - off) : space;

      (void)memcpy(&s_pageBuf[s_pageFill], &req[2 + off], chunk);
      s_pageFill += chunk;
      off        += (uint16_t)chunk;

      if (s_pageFill == OTA_PAGE_BUF_SIZE)
      {
        Ota_PageFlush(false);                       /* 满页落盘 */
        if (s_dl != OTA_DL_ACTIVE)
        {
          Ota_SendNrc(UDS_SID_TRANSFER_DATA, UDS_NRC_UPLOAD_DOWNLOAD_NOK);
          return;
        }
      }
    }
  }

  /* ★ 序号 1..255 循环，255 之后回到 1（0 永远非法），与 ota_common.h 的宏一致 */
  s_expectedSeq = (uint8_t)((seq >= UDS_BLOCK_SEQ_MAX) ? UDS_BLOCK_SEQ_WRAP_TO : (seq + 1U));

  {
    uint8_t resp[2];
    resp[0] = 0x76U;
    resp[1] = seq;
    Ota_Send(resp, 2U);
  }
}

/*--------------------------------------- 0x37 RequestTransferExit ----------------------*/
void Ota_OnTransferExit(const uint8_t *req, uint16_t len)
{
  if (s_dl != OTA_DL_ACTIVE)
  {
    Ota_SendNrc(UDS_SID_TRANSFER_EXIT, UDS_NRC_CONDITIONS);
    return;
  }

  if (len == 5U)                                    /* 可选携带 4 字节 CRC32 */
  {
    s_exitCrc      = U32BE(&req[1]);
    s_exitCrcValid = true;
  }

  Ota_PageFlush(true);                              /* 残余字节以 0xFF 补齐后落盘 */
  if (s_dl != OTA_DL_ACTIVE)
  {
    Ota_SendNrc(UDS_SID_TRANSFER_EXIT, UDS_NRC_UPLOAD_DOWNLOAD_NOK);
    return;
  }

  s_dl = OTA_DL_COMPLETE;

  {
    uint8_t resp[2];
    resp[0] = 0x77U;
    resp[1] = 0x00U;
    Ota_Send(resp, 2U);
  }
}

/**
 * @brief  在目标槽首合成 32B 镜像头 + 0x80 保留区填充，并回读校验
 * @note   协议契约（ota/docs/03 §145-148）：0x34/0x36 传输流**只含纯 payload**，
 *         槽首镜像头由设备在 FF02 激活时自己合成。上位机不得把头塞进数据流。
 *         没有这一步，即使 payload 全部写对，BL 的 BL_ImageValidate() 也会因
 *         槽首找不到 OTA_IMG_MAGIC 而返回 BL_IMG_ERR_HDR_MAGIC —— **永不跳转**。
 * @param  slot   目标槽（0=A, 1=B）
 * @param  total  payload 字节数（= s_received = s_declaredSize）
 * @param  crc    payload CRC32（= 0x37 携带值优先，缺省用本轮实算值）
 * @retval true 写入并回读校验通过
 */
static bool Ota_SynthImageHeader(uint8_t slot, uint32_t total, uint32_t crc)
{
  uint8_t  hdr[OTA_IMG_HDR_SIZE];               /* 32B 头 + [32,0x80) 填充 */
  uint32_t slotBase = Ota_SlotBase(slot);
  uint32_t loadAddr = Ota_AppBase(slot);
  uint32_t hdrCrc;

  (void)memset(hdr, 0xFFU, sizeof(hdr));        /* 填充区先全 0xFF，再写头 */

  PutU32LE(&hdr[OTA_IMG_OFF_MAGIC],        OTA_IMG_MAGIC);
  /* ★★【2026-10-18 根因修复】imgSize 必须等于 payloadSize + OTA_IMG_HDR_SIZE(0x80)。
   *   原写成 OTA_IMG_HDR_STRUCT_SIZE(32) + total，比正确值小 0x60。
   *   Bootloader 的 BL_ImageReadHeader() 明确要求
   *       imgSize == payloadSize + OTA_IMG_HDR_SIZE
   *   不满足即返回 BL_IMG_ERR_SIZE_RANGE(=4)，于是：
   *   FF02 明明成功（TRIAL 标志、trialLeft 都正确落盘），复位后 BL 却判
   *       "slot 1 image invalid (err=0x00000004)" → 回退到旧槽，
   *   表现为"OTA 全流程跑完但新固件不生效"。
   *   （工厂镜像脚本 make_factory_image.py 一直写的是 0x80，所以 Slot A 正常，
   *     只有走 0x34/0x36 之后由设备自己合成的头才是错的。） */
  PutU32LE(&hdr[OTA_IMG_OFF_IMG_SIZE],     OTA_IMG_HDR_SIZE + total);
  PutU32LE(&hdr[OTA_IMG_OFF_PAYLOAD_SIZE], total);
  /* version 缺省 0（协议允许；后续可经 0x2E DID 下发） */
  hdr[OTA_IMG_OFF_VERSION + 0U] = 0x00U;
  hdr[OTA_IMG_OFF_VERSION + 1U] = 0x00U;
  PutU32LE(&hdr[OTA_IMG_OFF_LOAD_ADDR],    loadAddr);
  PutU32LE(&hdr[OTA_IMG_OFF_PAYLOAD_CRC],  crc);
  PutU32LE(&hdr[OTA_IMG_OFF_RESERVED],     0x00000000U);

  /* hdrCrc = CRC32(hdr[0,24))；须在写入 hdrCrc 字段之前算完 */
  hdrCrc = Ota_Crc32(hdr, OTA_IMG_OFF_HDR_CRC);
  PutU32LE(&hdr[OTA_IMG_OFF_HDR_CRC], hdrCrc);

  if (Ota_FlashWrite(slotBase, hdr, (uint32_t)sizeof(hdr)) != OTA_FL_OK)
  {
    BSP_UART_Printf("[ota] SYNTH hdr: flash write FAIL @0x%08lX\r\n",
                    (unsigned long)slotBase);
    s_cfg.lastError = OTA_ERR_PROGRAM;
    s_actErr = OTA_ACT_ERR_FLASH;
    return false;
  }

  /* 回读校验：magic / payloadSize / loadAddr / hdrCrc 四项全对才算成功 */
  {
    const uint8_t *rd = (const uint8_t *)slotBase;
    uint32_t rdMagic = RdU32LE(rd + OTA_IMG_OFF_MAGIC);
    uint32_t rdPSize = RdU32LE(rd + OTA_IMG_OFF_PAYLOAD_SIZE);
    uint32_t rdLoad  = RdU32LE(rd + OTA_IMG_OFF_LOAD_ADDR);
    uint32_t rdCrc   = RdU32LE(rd + OTA_IMG_OFF_HDR_CRC);
    uint32_t calcCrc = Ota_Crc32(rd, OTA_IMG_OFF_HDR_CRC);

    BSP_UART_Printf("[ota] SYNTH rd: magic=%08lX want=%08lX | ps=%lu want=%lu | la=%08lX want=%08lX | hc=%08lX calc=%08lX\r\n",
                    (unsigned long)rdMagic, (unsigned long)OTA_IMG_MAGIC,
                    (unsigned long)rdPSize,  (unsigned long)total,
                    (unsigned long)rdLoad,   (unsigned long)loadAddr,
                    (unsigned long)rdCrc,    (unsigned long)calcCrc);
    if (rdMagic != OTA_IMG_MAGIC) { s_actErr = OTA_ACT_ERR_MAGIC;   return false; }
    if (rdPSize != total)         { s_actErr = OTA_ACT_ERR_PSIZE;   return false; }
    if (rdLoad  != loadAddr)      { s_actErr = OTA_ACT_ERR_LOADADDR;return false; }
    if (rdCrc   != calcCrc)       { s_actErr = OTA_ACT_ERR_HDRCRC;  return false; }
  }
  return true;
}

/*--------------------------------------- 0x31 RoutineControl ---------------------------*/
static void Ota_RoutineResult(uint16_t rid, uint8_t result)
{
  uint8_t resp[5];
  resp[0] = 0x71U;
  resp[1] = UDS_RC_START;
  resp[2] = (uint8_t)(rid >> 8);
  resp[3] = (uint8_t)(rid & 0xFFU);
  resp[4] = result;
  Ota_Send(resp, 5U);
}

/**
  * @brief  FF01 专用应答：[71, 01, FF, 01, status, crc32(4B 大端)]，共 9 字节
  * @note   带上设备实算 CRC 便于上位机交叉核对（见 ota_common.h 的
  *         UDS_VERIFY_RESP_FULL_LEN / MIN_LEN 兼容性说明）
  */
static void Ota_RoutineResultCrc(uint16_t rid, uint8_t result, uint32_t crc)
{
  uint8_t resp[UDS_VERIFY_RESP_FULL_LEN];
  resp[0] = 0x71U;
  resp[1] = UDS_RC_START;
  resp[2] = (uint8_t)(rid >> 8);
  resp[3] = (uint8_t)(rid & 0xFFU);
  resp[4] = result;
  PutU32BE(&resp[5], crc);
  Ota_Send(resp, UDS_VERIFY_RESP_FULL_LEN);
}

void Ota_OnRoutineControl(const uint8_t *req, uint16_t len)
{
  uint16_t rid;

  if (len < 4U)
  {
    Ota_SendNrc(UDS_SID_ROUTINE_CTRL, UDS_NRC_INCORRECT_MSG_LEN);
    return;
  }
  if ((req[1] & 0x7FU) != UDS_RC_START)             /* 仅支持 startRoutine */
  {
    Ota_SendNrc(UDS_SID_ROUTINE_CTRL, UDS_NRC_SUBFUNC_NOT_SUP);
    return;
  }

  rid = U16BE(&req[2]);

  switch (rid)
  {
    case UDS_RID_ERASE:
    {
      /* FF00：显式擦除目标槽。长度字段沿用 BL 侧契约的 2 字节大端（≤64KB）；
       * 完整槽升级请直接走 0x34（它会自动按 size 擦除），FF00 主要用于"只擦不传/单独准备"。 */
      uint32_t size = (len >= 6U) ? U16BE(&req[4]) : 0U;
      uint8_t  tgt  = (uint8_t)(s_runningSlot ^ 1U);
      if (size == 0U)
      {
        Ota_SendNrc(UDS_SID_ROUTINE_CTRL, UDS_NRC_REQUEST_RANGE);
        return;
      }
      s_targetSlot   = tgt;
      s_targetAddr   = Ota_AppBase(tgt);
      s_declaredSize = size;
      s_received     = 0U;
      s_runningCrc   = 0xFFFFFFFFU;
      s_pageFill     = 0U;
      s_expectedSeq  = 1U;
      s_exitCrcValid = false;
      /* 【2026-10-12】同 0x34：Ota_AppBase(tgt) = 槽基址+0x80 非页对齐，
       *   必须向下对齐后再交给要求页对齐的擦除函数。 */
      {
        uint32_t eraseAddr = s_targetAddr & ~(OTA_FLASH_PAGE_SIZE - 1U);
        uint32_t eraseSize = (s_targetAddr - eraseAddr) + size;
        s_targetAddr = eraseAddr;                    /* 后续写入也从页首开始对齐写 */
        s_declaredSize = eraseSize;
        if (Ota_FlashEraseRange(eraseAddr, eraseSize) == OTA_FL_OK)
        {
          s_dl = OTA_DL_ACTIVE;                       /* 允许随后直接进入 0x36 */
          Ota_RoutineResult(rid, 0x00U);
        }
        else
        {
          s_cfg.lastError = OTA_ERR_ERASE;
          Ota_RoutineResult(rid, 0x01U);
        }
      }
      break;
    }

    case UDS_RID_VERIFY:
    {
      /* FF01：CRC32 校验。对本轮"实际接收到的字节流"重算（续传时即尾部区段），
       * 优先与 0x37 携带值比对；未携带则与镜像头登记值比对（全量下载时成立）。 */
      uint32_t crc    = ~s_runningCrc;
      uint8_t  result = 0x00U;

      if (s_received != s_declaredSize)
      {
        result = 0x02U;                             /* 长度不符 */
      }
      else if (s_exitCrcValid)
      {
        if (crc != s_exitCrc)
        {
          result = 0x01U;                           /* 与上位机声明的 CRC 不符 */
        }
      }
      else if ((s_cfg.slotCrc[s_targetSlot] != 0U) && (crc != s_cfg.slotCrc[s_targetSlot]))
      {
        result = 0x01U;                             /* 与镜像头登记值不符（仅全量下载语义） */
      }
      else
      {
        /* 校验通过 */
      }

      if (result == 0x00U)
      {
        s_cfg.slotCrc[s_targetSlot]  = crc;
        s_cfg.slotSize[s_targetSlot] = s_received;
        s_cfg.lastError              = OTA_ERR_NONE;
      }

      Ota_RoutineResultCrc(rid, result, crc);       /* 9 字节应答，带设备实算 CRC */
      break;
    }

    case UDS_RID_ACTIVATE:
    {
      /* FF02：置 TRIAL_<目标槽>、trialLeft = OTA_TRIAL_BOOT_LIMIT；随后由上位机发 0x11 01 复位。
       * 写入"另一个物理页"（与本槽所在控制区页不同），保证本次擦写不会破坏本槽自己的配置副本。 */
      uint32_t crc = ~s_runningCrc;
      uint32_t pageBase;
      uint32_t dstOff;
      OTA_Cfg  p0, p1, nv;

      if ((s_dl != OTA_DL_COMPLETE) || (s_received != s_declaredSize))
      {
        BSP_UART_Printf("ota: FF02 precheck dl=%u recv=%lu decl=%lu\r\n",
                        (unsigned)s_dl, (unsigned long)s_received,
                        (unsigned long)s_declaredSize);
        Ota_RoutineResult(rid, OTA_ACT_ERR_PRECHECK);
        break;
      }

      /* ★ 先合成槽首 32B 镜像头（+0x80 保留区填充）并回读校验。
       *   协议契约要求由设备自己合成（docs/03 §146-148）。**必须在写控制区之前**：
       *   一旦 TRIAL 标志落盘而头没写成功，复位后 BL 校验槽首找不到 MAGIC，
       *   会直接判该槽无效 → 表现为"OTA 完但没跳进新固件"。
       *   payloadCrc 优先用 0x37 携带值，缺省用本轮实算 CRC。 */
      if (!Ota_SynthImageHeader(s_targetSlot, s_received,
                                s_exitCrcValid ? s_exitCrc : (~s_runningCrc)))
      {
        BSP_UART_Printf("ota: FF02 synth FAILED actErr=0x%02X\r\n", s_actErr);
        Ota_RoutineResult(rid, s_actErr);        /* 合成/回读失败 → 不置 TRIAL */
        break;
      }

      /* ★★【2026-10-18 根因修复】控制区写目标页必须按**运行槽**的对侧 Bank 选，
       *   不能按目标槽选。
       *
       *   STM32G4 双 Bank：对**正在执行代码的那个 Bank** 做擦除/编程时，
       *   该 Bank 的所有取指都会被 ftfl 接口挂起 —— 同 Bank 擦写等于把 CPU 冻住，
       *   表现为 APP 在 FF02 直接复位、随后由 BL 接手（BL 日志
       *   `BL: synth header validate FAIL slot 1 (err 7)`）。
       *
       *   原实现写的是 `(s_targetSlot == 0U) ? MIRROR : PRIMARY`，
       *   而 targetSlot 恒等于 !runningSlot —— 于是**恰好总是选中正在运行的那一侧**：
       *     运行 Slot A(Bank1) → target=1 → 选 PRIMARY(0x08000000, Bank1) ❌
       *     运行 Slot B(Bank2) → target=0 → 选 MIRROR (0x08040000, Bank2) ❌
       *   改为按 runningSlot 反选，保证擦写永远发生在**另一个 Bank**：
       *     运行 Bank1 → 写 MIRROR(Bank2)；运行 Bank2 → 写 PRIMARY(Bank1)。 */
      pageBase = (s_runningSlot == 0U) ? OTA_CFG_MIRROR_ADDR : OTA_CFG_PRIMARY_ADDR;

      /* 1. 整页暂存到 s_pageBuf（不压栈） */
      (void)memcpy(s_pageBuf, (const void *)pageBase, OTA_CFG_PAGE_SIZE);
      (void)memcpy(&p0, &s_pageBuf[OTA_CFG_SLOT_OFFSET],     sizeof(p0));
      (void)memcpy(&p1, &s_pageBuf[OTA_CFG_SLOT_MIRROR_OFF], sizeof(p1));

      /* 2. 目标偏移 = 更旧/无效的那一份；另一份留在缓冲中随整页回写 */
      if (Ota_CfgValid(&p0) && Ota_CfgValid(&p1))
      {
        dstOff = (p0.seq <= p1.seq) ? OTA_CFG_SLOT_OFFSET : OTA_CFG_SLOT_MIRROR_OFF;
      }
      else if (Ota_CfgValid(&p0))
      {
        dstOff = OTA_CFG_SLOT_MIRROR_OFF;
      }
      else
      {
        dstOff = OTA_CFG_SLOT_OFFSET;
      }

      nv               = s_cfg;
      nv.slotCrc[s_targetSlot]  = crc;
      nv.slotSize[s_targetSlot] = s_received;
      nv.state                  = (s_targetSlot == 0U) ? OTA_ST_TRIAL_A : OTA_ST_TRIAL_B;
      nv.trialLeft              = OTA_TRIAL_BOOT_LIMIT;
      nv.lastError              = OTA_ERR_NONE;
      nv.seq++;
      nv.crc = Ota_Crc32((const uint8_t *)&nv, (uint32_t)offsetof(OTA_Cfg, crc));

      /* 3. 走控制区专用写路径（白名单放行控制区页；不能走 Ota_FlashWrite，
       *    因为它的 "< OTA_SLOT_A_ADDR 一律拒绝" 保护会挡住控制区本身） */
      if (Ota_CfgBlockWrite(pageBase, dstOff, &nv))
      {
        s_cfg = nv;
        Ota_RoutineResult(rid, 0x00U);
      }
      else
      {
        s_cfg.lastError = OTA_ERR_PROGRAM;
        Ota_RoutineResult(rid, OTA_ACT_ERR_CFGWRITE);
      }
      break;
    }

    case UDS_RID_ROLLBACK:
    {
      /* FF03：显式回滚 —— 切到另一槽并置稳态 */
      uint8_t other = (uint8_t)(s_runningSlot ^ 1U);
      if (s_cfg.slotSize[other] == 0U)
      {
        Ota_RoutineResult(rid, 0x01U);              /* 另一槽无效，无处可回 */
        break;
      }
      s_cfg.state     = (other == 0U) ? OTA_ST_IDLE_A : OTA_ST_IDLE_B;
      s_cfg.trialLeft = 0U;
      s_cfg.seq++;
      s_cfg.crc = Ota_Crc32((const uint8_t *)&s_cfg, (uint32_t)offsetof(OTA_Cfg, crc));
      Ota_RoutineResult(rid, 0x00U);
      break;
    }

    case UDS_RID_COMMIT:
      /* FF04：试运行确认（TRIAL -> IDLE），本服务的"正式归属"在 APP */
      Ota_RoutineResult(rid, Ota_ParamCommitTrial() ? 0x00U : 0x01U);
      break;

    default:
      Ota_SendNrc(UDS_SID_ROUTINE_CTRL, UDS_NRC_REQUEST_RANGE);
      break;
  }
}

/*--------------------------------------- 0x11 / 0x2E -----------------------------------*/
void Ota_OnEcuReset(const uint8_t *req, uint16_t len)
{
  if ((len != 2U) || ((req[1] & 0x7FU) != 0x01U))
  {
    Ota_SendNrc(UDS_SID_ECU_RESET, UDS_NRC_SUBFUNC_NOT_SUP);
    return;
  }

  {
    uint8_t resp[2];
    resp[0] = 0x51U;
    resp[1] = 0x01U;
    Ota_Send(resp, 2U);
  }

  /* 给 ISO-TP 与应答留出送达时间（与 CAN 上 1M 速率下一个 FF+CF 的量级相当，取保守值） */
  HAL_Delay(50U);

  s_bootReqPending = true;                          /* 复位后由 BL 的启动窗口接管 */
  NVIC_SystemReset();
  for (;;) { }                                      /* NVIC_SystemReset 不返回 */
}

void Ota_OnWriteBootReq(const uint8_t *req, uint16_t len)
{
  if ((len != 4U) || (U16BE(&req[1]) != UDS_DID_BOOT_REQ) || (req[3] != 0x01U))
  {
    Ota_SendNrc(UDS_SID_WRITE_DID, UDS_NRC_REQUEST_RANGE);
    return;
  }

  s_bootReqPending = true;                          /* RAM 标志：软复位后由 BL 启动窗口判定 */

  {
    uint8_t resp[3];
    resp[0] = 0x6EU;
    resp[1] = req[1];
    resp[2] = req[2];
    Ota_Send(resp, 3U);
  }
}

/*--------------------------------------- 0x22 只读 DID ---------------------------------*/
uint16_t Ota_ReadDid(uint16_t did, uint8_t *resp, uint16_t respMax)
{
  uint16_t n = 0U;

  if (respMax < 11U)
  {
    return 0U;
  }
  resp[n++] = 0x62U;
  resp[n++] = (uint8_t)(did >> 8);
  resp[n++] = (uint8_t)(did & 0xFFU);

  switch (did)
  {
    case UDS_DID_OTA_STATE:
      resp[n++] = (uint8_t)s_cfg.state;
      break;

    case UDS_DID_ACTIVE_SLOT:
      resp[n++] = s_runningSlot;
      break;

    case UDS_DID_OTA_VERSION:
      /* 4 字节：A 槽 major/minor + B 槽 major/minor（各 2 字节） */
      resp[n++] = (uint8_t)((s_cfg.slotVersion[0] >> 24) & 0xFFU);
      resp[n++] = (uint8_t)((s_cfg.slotVersion[0] >> 16) & 0xFFU);
      resp[n++] = (uint8_t)((s_cfg.slotVersion[1] >> 24) & 0xFFU);
      resp[n++] = (uint8_t)((s_cfg.slotVersion[1] >> 16) & 0xFFU);
      break;

    case UDS_DID_SLOT_CRC:
      PutU32BE(&resp[n], s_cfg.slotCrc[0]); n += 4U;
      PutU32BE(&resp[n], s_cfg.slotCrc[1]); n += 4U;
      break;

    case UDS_DID_CAPABILITY:
      resp[n++] = 0x07U;   /* bit0=0x34/36/37  bit1=0x31 例程  bit2=双槽 A/B */
      break;

    default:
      return 0U;         /* 不属于 OTA 的 DID，交回调用方 */
  }

  return n;
}

/*--------------------------------------- 初始化与任务 -----------------------------------*/
void Ota_Init(void)
{
  (void)Ota_ParamLoad();

  /* 本固件所在槽：由向量表基址反推（APP 的 SCB->VTOR = Flash 基址 + 槽偏移） */
  {
    uint32_t vtor = SCB->VTOR & 0xFFFFFC00U;
    if (vtor >= OTA_SLOT_B_ADDR)
    {
      s_runningSlot = 1U;
    }
    else
    {
      s_runningSlot = 0U;
    }
  }

  s_trialPending = ((s_cfg.state == OTA_ST_TRIAL_A) && (s_runningSlot == 0U)) ||
                   ((s_cfg.state == OTA_ST_TRIAL_B) && (s_runningSlot == 1U));
  s_selfCheckTick = HAL_GetTick();
  s_dl            = OTA_DL_IDLE;

  if (s_trialPending)
  {
    BSP_UART_Printf("ota: TRIAL boot (slot %u, left %u), self-check %ums\r\n",
                    (unsigned)s_runningSlot, (unsigned)s_cfg.trialLeft,
                    (unsigned)OTA_SELFCHECK_MS);
  }
  else
  {
    BSP_UART_Printf("ota: slot %u state %u\r\n",
                    (unsigned)s_runningSlot, (unsigned)s_cfg.state);
  }
}

/**
  * @brief  试运行自检：通信真的通了 + 稳定运行够久，才把新固件"转正"
  * @note   判据刻意用工程里现成的信号（CAN 统计与自检），不新增语义。
  *         未通过则不落定 —— 让 Bootloader 在后续复位中按 trialLeft 自动回滚。
  */
bool Ota_IsDownloading(void)
{
  return (s_dl == OTA_DL_ACTIVE);
}

void Ota_Task(void)
{
  if (!s_trialPending)
  {
    return;
  }
  if ((HAL_GetTick() - s_selfCheckTick) < OTA_SELFCHECK_MS)
  {
    return;
  }

  {
    const CAN_Stats *st = CAN_GetStats();
    if ((st->txAckCount > 0U) && (st->rxCount > 0U))
    {
      (void)Ota_ParamCommitTrial();
    }
    else
    {
      BSP_UART_Send("ota: self-check not passed, keep TRIAL\r\n", 39U);
      s_selfCheckTick = HAL_GetTick();               /* 下个窗口再评估 */
    }
  }
}

bool Ota_IsTrialBoot(void)  { return s_trialPending; }
uint8_t Ota_RunningSlot(void) { return s_runningSlot; }

void Ota_PrintStatus(void)
{
  BSP_UART_Printf("ota: slot=%u state=%u trialLeft=%u A(size=%lu crc=%08lX) B(size=%lu crc=%08lX) lastErr=%lu\r\n",
                  (unsigned)s_runningSlot, (unsigned)s_cfg.state, (unsigned)s_cfg.trialLeft,
                  (unsigned long)s_cfg.slotSize[0], (unsigned long)s_cfg.slotCrc[0],
                  (unsigned long)s_cfg.slotSize[1], (unsigned long)s_cfg.slotCrc[1],
                  (unsigned long)s_cfg.lastError);
}
