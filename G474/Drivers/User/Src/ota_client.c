/**
  ******************************************************************************
  * @file    ota_client.c
  * @brief   板A侧 OTA 客户端：板内自发起 UDS 下载序列（无需 PC 下发命令）
  *
  * 【为什么需要它】
  * 本工程的板载 USB 转串口 RX 方向已确认硬件开路（PA10 全程零跳变，见 MEMORY.md
  * 的 P2 记录），所有依赖串口下发命令的上位机工具（can_console.py 等）都用不了。
  * 而 CAN 通道已双向打通（A↔B 收发计数互相印证、busOff/errPassive 全 0），
  * 因此让板 A 用它已有的"UDS 客户端"角色（Uds_Init 里 CAN_NODE_ROLE==A 时
  * IsoTp_Init(RESP, REQ, Uds_ClientOnMsg, ...)）自己在总线上跑下载流程。
  *
  * 【UDS 下载序列】（ota_common.h 定义的服务）
  *   1) 0x3E TesterPresent        —— 探活 + 刷新 S3 会话
  *   2) 0x10 03 诊断会话           —— 进入可刷写会话（0x34 会检查会话）
  *   3) 0x34 RequestDownload      —— 34 00 44 + 4B 目标地址 + 4B 总长度（11 字节）
  *   4) 0x36 TransferData          —— 36 + seq(1..255 循环) + 载荷，逐块送
  *   5) 0x37 RequestTransferExit   —— 落定
  *   6) 0x31 RoutineControl       —— FF00 触发目标板复位进入新固件
  *
  * 【镜像数据来源】
  *   目标 bin 由 make_factory_image.py 生成（G474_*_factory.bin，含 32B 镜像头）。
  *   bin 被编进本模块的 const 数组（.rodata，不占 RAM），故要求
  *   APP_SELF_OTA_ENABLE=1 时才编译进来，避免默认固件无谓变大。
  *
  * 【安全】全程非阻塞：每次 App_OtaClient_Task() 只发一帧，等应答回来再走下一步。
  *        任一步超时（OTA_CLIENT_STEP_TIMEOUT_MS）即回到 S1 重来，绝不阻塞主循环，
  *        因此不会触发 IWDG 复位。
  ******************************************************************************
  */
#include "ota_client.h"
#include "isotp.h"
#include "uds.h"
#include "ota_common.h"
#include "bsp_uart.h"
#include <stdio.h>
#include <string.h>

/*--------------------------------------- 可配置项 --------------------------------------*/
#ifndef OTA_CLIENT_ENABLE
#define OTA_CLIENT_ENABLE         0        /* 默认关闭（需同时提供镜像数据） */
#endif

#if (OTA_CLIENT_ENABLE != 0)

/* 状态机 */
typedef enum
{
  OTA_CS_IDLE = 0,
  OTA_CS_PROBE,            /* 1. TesterPresent 探活 */
  OTA_CS_ENTER_SESSION,    /* 2. 0x10 03 */
  OTA_CS_QUERY_SLOT,       /* 3. 0x22 F181 问对端当前跑在哪个槽 */
  OTA_CS_DOWNLOAD,         /* 4. 0x34 RequestDownload */
  OTA_CS_TRANSFER,         /* 5. 0x36 逐块 */
  OTA_CS_EXIT,             /* 6. 0x37 TransferExit（携带 payload CRC32） */
  OTA_CS_VERIFY,           /* 7. 0x31 01 FF01 CRC 校验 */
  OTA_CS_ACTIVATE,         /* 8. 0x31 01 FF02 激活目标槽（设备在此合成镜像头） */
  OTA_CS_RESET,            /* 9. 0x11 01 让目标板复位进试运行 */
  OTA_CS_DONE,             /* 全流程完成 */
  OTA_CS_FAIL              /* 失败（会重试） */
} OtaClientState_t;

/* 可观测计数（用 SWD 直接读，不依赖串口） */
volatile uint32_t g_otaCliReqCnt;       /* 发出的请求帧数 */
volatile uint32_t g_otaCliRspCnt;       /* 收到的应答帧数 */
volatile uint32_t g_otaCliNrcCnt;       /* 收到 NRC 的次数 */
volatile uint8_t  g_otaCliState;        /* 当前状态（= OtaClientState_t） */
volatile uint8_t  g_otaCliBlockSeq;     /* 最近一次收到的成功块序号 */
volatile uint32_t g_otaCliBlocksOk;     /* 成功传输的块计数 */
volatile uint8_t  g_otaCliLastRsp[8];   /* 最近一次应答的前 8 字节 */
volatile uint32_t g_otaCliLastNrc;      /* 最近一次 NRC 码 */

/*--------------------------------------- 内部变量 --------------------------------------*/
static OtaClientState_t s_state = OTA_CS_IDLE;
static uint32_t          s_stateTick;        /* 进入当前状态的时刻 */
static uint32_t          s_sentTick;         /* 发出请求的时刻（等应答） */
static uint32_t          s_off;              /* 当前已传字节偏移 */
static uint8_t           s_seq;              /* 下一块的序号 */
static bool              s_rspPending;       /* 已发请求、等待应答中 */
/* 【2026-10-18 新增】0x36 单块重发计数。
 * ISO-TP 本身不做重传，而对端 FDCAN 的硬件 RX FIFO0 只有 3 项
 * （HAL 里 SRAMCAN_RF0_NBR=3，vendor 文件不可改），只要主循环被
 * UART/LCD 等阻塞一次，就可能丢掉某个 CF → 该块作废、应答不来。
 * 实测表现：传几块后必然卡住且 blk 值随机。
 * 对策：传输态超时**不判整轮失败**，而是原地重发同一块（seq 不变，
 * 对端 s_expectedSeq 也未推进，可直接接受）。 */
#define OTA_CLIENT_XFER_RETRY_MAX   6U
static uint8_t           s_xferRetry;
static uint32_t          s_retryCnt;

/* ★ 目标槽：由 0x22 F181 查出对端活动槽后取反得到。
 *   【2026-10-12 修复】此前硬编码刷 Slot B（OTA_SLOT_B_APP_ADDR），
 *   而 agent.c:539 有一条硬安全约束"只允许写非活动槽"，对活动槽一律回
 *   NRC 0x22 conditionsNotCorrect。一旦对端已经从 Slot B 在跑（例如上一轮
 *   OTA 成功激活并试运行通过），客户端就会陷入
 *   "0x34 -> 7F3422 -> FAIL -> 2s后退避 -> 再0x34" 的无限循环
 *   （实测 18 秒内 1340 次 0x34，0 次 TesterPresent —— 表现为"完全刷不上"）。 */
static uint8_t           s_targetSlot= 1U;    /* 0=A 1=B */
static uint32_t          s_targetAppAddr;      /* = 目标槽基址 + OTA_IMG_HDR_SIZE */
static uint8_t           s_unmatched;          /* 连续"无法推进状态机"的应答数 */

/* ============================================
 * 【2026-10-12 协议契约修正】
 * 0x34 的传输流**只含净 payload**，槽首 32B 镜像头由设备在 FF02 激活时合成
 * （见 ota/docs/03-OTA协议与刷写时序.md §145-148）。
 * 因此本客户端必须：
 *   addr = OTA_SLOT_B_ADDR + OTA_IMG_HDR_SIZE（= APP 链接基址 0x08040880）
 *   size = g_otaImageLen - OTA_IMG_HDR_SIZE（纯 payload）
 *   0x36 数据从 g_otaImage[OTA_IMG_HDR_SIZE] 开始取
 *   0x37 携带 payload 的 CRC32（4B BE），它同时供 FF01 校验与镜像头合成
 * ============================================ */
#define OTA_SLOT_B_APP_ADDR   (OTA_SLOT_B_ADDR + OTA_IMG_HDR_SIZE)

/* 传给目标板的镜像（含 32B 头）。文件由 scripts/gen_ota_image_c.py 生成。 */
extern const uint8_t g_otaImage[];         /* .rodata，编译器生成 */
extern const uint32_t g_otaImageLen;

#define OTA_CLIENT_STEP_TIMEOUT_MS   3000U   /* 单步超时 */
#define OTA_CLIENT_RSP_TIMEOUT_MS    1000U   /* 应答等待超时 */
/* 【2026-10-18 修复】0x34 的应答窗口必须单独放大：agent 收到 0x34 后要
 * 擦除整个目标槽（≈40 页 × 25ms ≈ 1s+，随固件大小增长），远超 1s。
 * ★ 关键：这个窗口必须**同时**被响应等待与全局守卫两处使用。
 *   原实现只在响应等待处用了 ×6，而全局守卫仍按 STEP_TIMEOUT_MS(3000ms)
 *   判定，于是守卫总在 3s 时先把 DOWNLOAD 判死，×6 窗口成了死代码。 */
#define OTA_CLIENT_DL_TIMEOUT_MS     (OTA_CLIENT_RSP_TIMEOUT_MS * 6U)
/* 传输态特例：一个 0x36 块要走 ISO-TP 多帧（FF + ~145 CF），
 * 还要等设备擦写 Flash 落盘，3s 不够用。 */
#define OTA_CLIENT_XFER_TIMEOUT_MS   30000U
/* 单块应答窗口：1010 字节 × 145 帧 × STmin 2ms ≈ 290ms，
 * 5s 已含 17 倍余量。窗口短才能让"丢帧重发"快速生效 ——
 * 用 30s 的话 6 次重试要 3 分钟。 */
#define OTA_CLIENT_BLOCK_TIMEOUT_MS   5000U

/* ★ 首次发起前的等待时间：必须 > 对端 BL 的 OTA_BOOT_WINDOW_MS(=1000ms)，
 *   否则请求落在对端启动窗口内会把它打进 recovery（见 IDLE 分支注释）。 */
#define OTA_CLIENT_START_DELAY_MS   5000U

/* 本轮传输的纯 payload 长度（不含 32B 镜像头） */
static uint32_t          s_payloadLen;
/* payload 起始偏移（跳过镜像头） */
#define OTA_PAYLOAD_OFF        OTA_IMG_HDR_SIZE

/* 本轮 payload 的 CRC32（0x37 携带，同时供 FF01 交叉校验） */
static uint32_t          s_payloadCrc;

/* ISO-TP 忙时的待发请求缓存。
 * ★**必须拷贝，不能只存指针**：
 *   各case 里的请求缓冲多为**栈上数组**（ENTER_SESSION 的 req[2]、DOWNLOAD 的
 *   req[11]、EXIT/VERIFY/ACTIVATE/RESET 的小数组），s_sendReq 返回后这些栈帧
 *   就失效了。若只存指针，下一拍 Task 重投时读的是已被复用的栈垃圾 —— 表现为
 *   "发出去的 0x34 内容随机变化 / 对端回 NRC 0x31 0x22"。
 *   只有 PROBE 的 static const 与 TRANSFER 的 s_xferReq 天然可以存指针，
 *   但为行为一致，这里统一拷贝。代价是 1010 字节常驻 RAM，可接受。 */
#define OTA_Q_MAX          (2U + OTA_CLIENT_CHUNK)   /* 0x36 请求是最大的那种 */
static uint8_t           s_queuedBuf[OTA_Q_MAX];
static uint16_t          s_queuedLen;
static bool              s_haveQueued;
volatile uint32_t        g_otaCliReqQueued;  /* 因 ISO-TP 忙而排队的次数（SWD 可观测） */

/*--------------------------------------- CRC32（标准 CRC-32/ISO-HDLC）---------------*/
/* 与 ota_agent.c 的 Ota_Crc32 同算法（该版本为 static，此处独立实现一份）。
 * 自检向量：CRC32("123456789") == 0xCBF43926 */
static uint32_t s_crc32(const uint8_t *data, uint32_t len)
{
  uint32_t crc = 0xFFFFFFFFU;
  for (uint32_t i = 0U; i < len; i++)
  {
    crc ^= data[i];
    for (uint32_t b = 0U; b < 8U; b++)
    {
      crc = ((crc & 1U) != 0U) ? ((crc >> 1) ^ 0xEDB88320U) : (crc >> 1);
    }
  }
  return ~crc;
}

/*--------------------------------------- 工具 --------------------------------------*/
static uint32_t s_now(void)
{
  return HAL_GetTick();
}

static bool s_elapsed(uint32_t mark, uint32_t span)
{
  return ((s_now() - mark) >= span);
}

/* ★ 只在状态**跃迁**时打印。排障时逐拍打印会把 115200 波特率刷满，
   * 真正的阶段日志（start OTA / peer active slot / NRC）全被挤出缓冲，
   * 造成"日志里只有 0x34 刷屏、看不到任何上下文"的假象（本轮踩过两次）。
   * 定位手段一律用 SWD 读 s_state / g_otaCliReqQueued / g_otaCliNrcCnt。 */
static void s_go(OtaClientState_t st)
{
  if (st != s_state)
  {
    BSP_UART_Printf("[ota] %u->%u (req=%lu rsp=%lu nrc=%lu q=%lu)\r\n",
                    (unsigned)s_state, (unsigned)st,
                    (unsigned long)g_otaCliReqCnt,
                    (unsigned long)g_otaCliRspCnt,
                    (unsigned long)g_otaCliNrcCnt,
                    (unsigned long)g_otaCliReqQueued);
  }
  s_state       = st;
  s_stateTick   = s_now();
  s_rspPending  = false;
  g_otaCliState = (uint8_t)st;
}

/*--------------------------------------- 应答回调 ------------------------------------*/
/* 由 uds.c 的 Uds_ClientOnMsg() 经 IsoTp 拼装完整帧后回调（板A 侧） */
void OtaClient_OnResp(const uint8_t *rsp, uint16_t len)
{
  g_otaCliRspCnt++;

  if ((rsp == NULL) || (len < 1U))
  {
    return;
  }

  /* 记录最近应答（SWD 可观测） */
  for (uint8_t i = 0U; (i < 8U) && (i < len); i++)
  {
    g_otaCliLastRsp[i] = rsp[i];
  }

  /* ---- 负响应（NRC）判定 ----
   * ★★【2026-10-18 根因修复】原判据 (rsp[0] & 0x80U) != 0U 是错的：
   *   UDS 负响应首字节恒为 0x7F，bit7 = 0，该条件**永远不成立**，
   *   于是整段 NRC 处理成了死代码。同时原码把 NRC 取成 rsp[1]（那是 SID），
   *   正确布局是 7F <SID> <NRC>。
   *
   *   后果链（实测）：
   *     1) agent 收到 0x34 后要擦整槽（>1s），按规范先回 7F 34 78；
   *     2) 客户端不认识 78，把它当作"无法推进状态机的应答"，
   *        连续 3 条即 give up；且清掉 s_rspPending 后下一拍立刻重发 0x34；
   *     3) 重发撞上仍在进行的擦除 → 擦除重启 → OTA 永远停在 step 4，
   *        并伴随 0x34 无限重试风暴。
   *
   *   正确判据：负响应 rsp[0] == 0x7F；正响应 rsp[0] == 请求SID + 0x40。 */
  if ((len >= 3U) && (rsp[0] == 0x7FU))
  {
    g_otaCliNrcCnt++;
    g_otaCliLastNrc = rsp[2];

    if (rsp[2] == 0x78U)
    {
      /* responsePending：设备正在忙（擦除/校验/写 Flash），继续等，绝不重发。
       * 刷新 s_sentTick 让 P2* 应答窗口重新计时（标准做法）。 */
      s_sentTick   = s_now();
      s_rspPending = true;
      s_unmatched  = 0U;

      {
        static uint32_t s_pndCnt;
        if ((s_pndCnt++ % 10U) == 0U)
        {
          BSP_UART_Printf("[ota] pending(0x78) state=%u len=%u\r\n",
                          (unsigned)s_state, (unsigned)len);
        }
      }
      return;
    }

    /* 其它 NRC：确定失败。完整打印 7F <SID> <NRC> 便于定位。 */
    s_rspPending = false;
    BSP_UART_Printf("[ota] NRC in step %u: 7F %02X %02X\r\n",
                    (unsigned)s_state, rsp[1], rsp[2]);
    s_go(OTA_CS_FAIL);
    return;
  }

  /* ★ 兜底：收到一条**无法推进状态机**的应答时必须判失败。
   *   原实现里非匹配应答只是"什么都不做"就返回，但下面已把 s_rspPending
   *   清零 —— 于是主循环下一拍立刻重发同一条请求，形成无限重试风暴
   *   （实测 reqCnt=162195 / rspCnt=53120，比值 3:1，retryCnt 只有 2）。
   *   典型触发：对端由 Bootloader 应答（bl_diag.c 也实现 0x34/0x36/0x37，
   *   但会话与区间规则与 APP 的 ota_agent.c 不同），或对端回了别的 SID。
   *   连续未识别 3 次即放弃，避免把对端刷死。 */
  if (s_unmatched < 3U)
  {
    s_unmatched++;
  }
  if (s_unmatched >= 3U)
  {
    BSP_UART_Printf("[ota] unmatched rsp in state %u (rsp0=0x%02X), give up\r\n",
                    (unsigned)s_state, rsp[0]);
    s_unmatched = 0U;
    s_go(OTA_CS_FAIL);
    return;
  }

  s_rspPending = false;
  s_unmatched   = 0U;      /* 走到这里说明应答可识别，清零计数 */

  switch (s_state)
  {
    case OTA_CS_PROBE:
      if (rsp[0] == (UDS_SID_TESTER_PRESENT + 0x40U))
      {
        BSP_UART_Printf("[ota] tester present OK\r\n");
        s_go(OTA_CS_ENTER_SESSION);
      }
      break;

    case OTA_CS_ENTER_SESSION:
      if (rsp[0] == (UDS_SID_DIAG_SESSION + 0x40U))
      {
        BSP_UART_Printf("[ota] session=0x%02X OK\r\n", UDS_SESSION_EXTENDED);
        s_off = 0U;
        s_seq = UDS_BLOCK_SEQ_MIN;
        s_go(OTA_CS_QUERY_SLOT);
      }
      break;

    /* 【2026-10-12 新增】先问对端活动槽，再决定刷哪一槽。
     * 不问就直接刷 Slot B 的话，对端一旦已在跑 Slot B 就会回 NRC 0x22，
     * 客户端陷入无限重试（实测 18s 内 1340 次 0x34 却一步都没推进）。
     * 应答格式：[62, F1, 81, activeSlot] */
    case OTA_CS_QUERY_SLOT:
      if ((rsp[0] == (UDS_SID_READ_DID + 0x40U)) && (len >= 4U) &&
          (rsp[1] == (uint8_t)(UDS_DID_ACTIVE_SLOT >> 8)) &&
          (rsp[2] == (uint8_t)(UDS_DID_ACTIVE_SLOT & 0xFFU)))
      {
        uint8_t peerSlot = (rsp[3] & 0x01U);      /* 0=A 1=B */
        s_targetSlot    = (uint8_t)(peerSlot ^ 1U);/* 只能写非活动槽 */
        s_targetAppAddr = (s_targetSlot == 0U) ? (OTA_SLOT_A_ADDR + OTA_IMG_HDR_SIZE)
                                               : OTA_SLOT_B_APP_ADDR;
        BSP_UART_Printf("[ota] peer active slot=%u -> target slot=%u addr=0x%08lX\r\n",
                        (unsigned)peerSlot, (unsigned)s_targetSlot,
                        (unsigned long)s_targetAppAddr);
        s_go(OTA_CS_DOWNLOAD);
      }
      else
      {
        BSP_UART_Printf("[ota] active slot query failed len=%u, assume target=B\r\n",
                        (unsigned)len);
        s_targetSlot    = 1U;
        s_targetAppAddr = OTA_SLOT_B_APP_ADDR;
        s_go(OTA_CS_DOWNLOAD);
      }
      break;

    case OTA_CS_DOWNLOAD:
      if (rsp[0] == (UDS_SID_REQUEST_DOWNLOAD + 0x40U))
      {
        BSP_UART_Printf("[ota] download OK, payload=%lu\r\n",
                        (unsigned long)s_payloadLen);
        s_xferRetry = 0U;
        s_go(OTA_CS_TRANSFER);
      }
      break;

    case OTA_CS_TRANSFER:
      if (rsp[0] == (UDS_SID_TRANSFER_DATA + 0x40U))
      {
        g_otaCliBlocksOk++;
        s_xferRetry = 0U;                    /* ★ 成功即清零重试计数 */
        s_off += OTA_CLIENT_CHUNK;
        s_seq  = UDS_BLOCK_SEQ_OF(s_seq + 1U);

        if (s_off >= s_payloadLen)
        {
          BSP_UART_Printf("[ota] all %lu bytes sent\r\n",
                          (unsigned long)s_payloadLen);
          s_go(OTA_CS_EXIT);
        }
      }
      break;

    case OTA_CS_EXIT:
      if (rsp[0] == (UDS_SID_TRANSFER_EXIT + 0x40U))
      {
        BSP_UART_Printf("[ota] transfer exit OK, crc=0x%08lX\r\n",
                        (unsigned long)s_payloadCrc);
        s_go(OTA_CS_VERIFY);
      }
      break;

    case OTA_CS_VERIFY:
      if (rsp[0] == (UDS_SID_ROUTINE_CTRL + 0x40U))
      {
        /* [71,01,FF,01,result,crc32(4B BE)]；result!=0 即校验失败 */
        uint8_t result = (len >= 5U) ? rsp[4] : 0xFFU;
        BSP_UART_Printf("[ota] FF01 rsp len=%u:", (unsigned)len);
        for (uint16_t i = 0U; i < len; i++) { BSP_UART_Printf(" %02X", rsp[i]); }
        BSP_UART_Printf("\r\n");
        if (result != 0x00U)
        {
          BSP_UART_Printf("[ota] FF01 VERIFY FAILED result=0x%02X\r\n", result);
          s_go(OTA_CS_FAIL);
        }
        else
        {
          BSP_UART_Printf("[ota] FF01 verify OK\r\n");
          s_go(OTA_CS_ACTIVATE);
        }
      }
      break;

    case OTA_CS_ACTIVATE:
      if (rsp[0] == (UDS_SID_ROUTINE_CTRL + 0x40U))
      {
        uint8_t result = (len >= 5U) ? rsp[4] : 0xFFU;
        BSP_UART_Printf("[ota] FF02 rsp len=%u:", (unsigned)len);
        for (uint16_t i = 0U; i < len; i++) { BSP_UART_Printf(" %02X", rsp[i]); }
        BSP_UART_Printf("\r\n");
        if (result != 0x00U)
        {
          BSP_UART_Printf("[ota] FF02 ACTIVATE FAILED result=0x%02X (0x11=flash 0x12=magic 0x13=psize 0x14=loadaddr 0x15=hdcrc 0x16=cfg 0x10=precheck)\r\n", result);
          s_go(OTA_CS_FAIL);
        }
        else
        {
          BSP_UART_Printf("[ota] FF02 activate OK, requesting peer reset\r\n");
          s_go(OTA_CS_RESET);
        }
      }
      break;

    case OTA_CS_RESET:
      if (rsp[0] == (UDS_SID_ECU_RESET + 0x40U))
      {
        BSP_UART_Printf("[ota] peer reset sent, flow DONE\r\n");
        s_go(OTA_CS_DONE);
      }
      else if (rsp[0] == (UDS_SID_ROUTINE_CTRL + 0x40U))
      {
        BSP_UART_Printf("[ota] target reset requested, flow DONE\r\n");
        s_go(OTA_CS_DONE);
      }
      break;

    default:
      break;
  }
}

/*--------------------------------------- 主流程 --------------------------------------*/
static void s_sendReq(const uint8_t *req, uint16_t len)
{
  if (Uds_ClientRequest(req, len) == ISOTP_OK)
  {
    g_otaCliReqCnt++;
    s_sentTick    = s_now();
    s_rspPending  = true;
    s_haveQueued  = false;
  }
  else
  {
    /* ISO-TP 忙（上一帧多帧还没发完）。**必须把请求挂起并拷贝**：
     * 若只是丢弃、等下一轮重新构造，0x34 会被无限重发（实测 1340 次/18s），
     * 且每次都撞上正在发送的上一帧 → 状态机永远推进不了。
     * 拷贝而非存指针的理由见 s_queuedBuf 的注释。 */
    if (len <= OTA_Q_MAX)
    {
      (void)memcpy(s_queuedBuf, req, len);
      s_queuedLen  = len;
      s_haveQueued = true;
      g_otaCliReqQueued++;
    }
  }
}

/*--------------------------------------- 客户端 ISO-TP 错误回调 -----------------------*/
/* IsoTp 在 N_Bs/N_Cr 超时或 SN 出错时回调。
 * 注意：**不**在此直接 s_go(OTA_CS_FAIL) —— 该回调在 CAN 中断/轮询上下文中触发，
 * 而 OTA 状态机的推进必须留在 OtaClient_Task 的主循环上下文。
 * 这里只清"等应答"标志并置一个错误码，由 Task 下一拍统一处理。 */
static volatile uint8_t s_isotpErr;             /* 最近一次 ISO-TP 错误码（0=无）*/

void OtaClient_OnIsoTpErr(uint8_t err)
{
  BSP_UART_Printf("[ota] isotp err=0x%02X\r\n", err);
  s_isotpErr = err;
}

static void s_startSequence(void)
{
  static const uint8_t probe[] = {UDS_SID_TESTER_PRESENT, 0x00U};

  /* 镜像头由设备合成，故本端只算纯 payload 的长度与 CRC */
  s_payloadLen = (g_otaImageLen > OTA_IMG_HDR_SIZE)
                     ? (g_otaImageLen - OTA_IMG_HDR_SIZE) : 0U;
  s_payloadCrc = s_crc32(&g_otaImage[OTA_PAYLOAD_OFF], s_payloadLen);

  BSP_UART_Printf("[ota] === start OTA to peer, payload=%lu crc=0x%08lX ===\r\n",
                  (unsigned long)s_payloadLen, (unsigned long)s_payloadCrc);
  s_off = 0U;
  s_seq = UDS_BLOCK_SEQ_MIN;
  s_go(OTA_CS_PROBE);
  s_sendReq(probe, sizeof(probe));
}

void OtaClient_Task(void)
{
  if (s_state == OTA_CS_DONE)
  {
    return;
  }

  /* ① 先把因 ISO-TP 忙而挂起的请求重投出去。
   * 顺序很关键：必须在超时判定**之前**，否则一个已经排队但还没发出去的请求
   * 会被超时逻辑误判为"没发出"。 */
  if (s_haveQueued && !s_rspPending)
  {
    if (Uds_ClientRequest(s_queuedBuf, s_queuedLen) == ISOTP_OK)
    {
      g_otaCliReqCnt++;
      s_sentTick   = s_now();
      s_rspPending = true;
      s_haveQueued = false;
    }
  }

  /* ② ISO-TP 错误（超时/SN 出错）由回调置位，在此统一踢出僵局。
   * 收尾后立刻 return：本拍的状态切换已由 s_go 完成。 */
  if (s_isotpErr != 0U)
  {
    BSP_UART_Printf("[ota] step %u aborted by isotp err=0x%02X\r\n",
                    (unsigned)s_state, s_isotpErr);
    s_isotpErr = 0U;
    s_rspPending = false;
    s_haveQueued = false;
    s_go(OTA_CS_FAIL);
    return;
  }

  /* 超时基准 = "最近一次成功发出请求"的时刻 s_sentTick。
   * ⚠️ 两个历史坑：
   *  1) 曾用 s_stateTick（仅在 s_go() 更新）。而 OTA_CS_TRANSFER 是一个持续
   *     ~79 个块的长状态，累计 3s 就被误判超时（实测每轮稳定卡在 105 块）。
   *  2) 改用 "s_rspPending ? s_sentTick : s_stateTick" 同样错：应答一到
   *     s_rspPending 就变 false，立刻回退到同样永不刷新的 s_stateTick。
   * 正确做法：任何时刻只要发出过请求就以 s_sentTick 为准；
   * 还没发出过（IsoTp 忙）时才看 s_stateTick 防永久卡在 BUSY。 */
  {
    uint32_t guardStart = s_sentTick;
    if (s_sentTick == 0U)
    {
      guardStart = s_stateTick;
    }
    uint32_t limitMs;
    if (s_state == OTA_CS_TRANSFER)
    {
      limitMs = OTA_CLIENT_BLOCK_TIMEOUT_MS;
    }
    else if (s_state == OTA_CS_DOWNLOAD)
    {
      limitMs = OTA_CLIENT_DL_TIMEOUT_MS;      /* ★ 与响应窗口保持一致 */
    }
    else
    {
      limitMs = OTA_CLIENT_STEP_TIMEOUT_MS;
    }
    if ((s_state != OTA_CS_IDLE) &&
        (s_state != OTA_CS_FAIL) &&
        (s_state != OTA_CS_DONE) &&
        s_elapsed(guardStart, limitMs))
    {
      /* ★ 传输态：单块超时只重发该块，不放弃整轮。
       *   对端在块失败后 s_expectedSeq 未推进，重发同一 seq 会被直接接受。 */
      if ((s_state == OTA_CS_TRANSFER) && (s_xferRetry < OTA_CLIENT_XFER_RETRY_MAX))
      {
        s_xferRetry++;
        s_rspPending = false;
        BSP_UART_Printf("[ota] block retry %u/%u off=%lu seq=%u\r\n",
                        (unsigned)s_xferRetry, (unsigned)OTA_CLIENT_XFER_RETRY_MAX,
                        (unsigned long)s_off, (unsigned)UDS_BLOCK_SEQ_OF(s_seq));
      }
      else
      {
        BSP_UART_Printf("[ota] step %u timeout (req=%lu rsp=%lu blk=%lu)\r\n",
                        (unsigned)s_state,
                        (unsigned long)g_otaCliReqCnt,
                        (unsigned long)g_otaCliRspCnt,
                        (unsigned long)g_otaCliBlocksOk);
        s_go(OTA_CS_FAIL);
      }
    }
  }

  switch (s_state)
  {
    case OTA_CS_IDLE:
    case OTA_CS_FAIL:
      /* ★★ 首次启动必须等对端从 Bootloader 跳进 APP ★★
       * 对端 BL 上电后有 OTA_BOOT_WINDOW_MS(=1000ms) 的启动窗口，
       * 期间收到诊断请求就置 s_recovery=true 并**主动不跳 APP**
       * （BL main.c:137/:173）。若本端一上电就发 0x3E/0x34，请求正好落在
       * 那个窗口里 → 对端永远停在 BL，由 BL 的 bl_diag.c 应答
       * （实测由此收到 7F 34 31 / 7F 34 22，而非 APP agent 的应答），
       * 表现为"OTA 永远推进不了 + 客户端重试风暴"。
       * 故首次序列延迟 5s（BL 窗口 1s + BL 跳转校验时间，余量充足）。 */
      if (s_retryCnt == 0U)
      {
        if (s_elapsed(s_stateTick, OTA_CLIENT_START_DELAY_MS))
        {
          s_retryCnt++;
          s_startSequence();
        }
        break;
      }

      /* FAIL 后退避 2s 再整体重来。
       * ⚠️ 退避量不能对 IDLE 取 0：s_elapsed 恒为真会让主循环每一拍都重跑
       * s_startSequence()，把状态机在 PROBE↔DOWNLOAD 之间高速振荡
       * （实测 hb 的 st 在 0~12 全范围乱跳、0x34 打印 415 次）。 */
      if (s_elapsed(s_stateTick, 2000U))
      {
        s_retryCnt++;
        s_startSequence();
      }
      break;

    case OTA_CS_PROBE:
    case OTA_CS_ENTER_SESSION:
    case OTA_CS_QUERY_SLOT:
    case OTA_CS_DOWNLOAD:
    case OTA_CS_TRANSFER:
    case OTA_CS_EXIT:
    case OTA_CS_RESET:
    case OTA_CS_VERIFY:
    case OTA_CS_ACTIVATE:
      {
        /* 传输态的应答窗口要覆盖「145 帧 ISO-TP 多帧 + agent 侧 Flash 落盘」 */
        uint32_t rspTimeoutMs = (s_state == OTA_CS_TRANSFER)
                                ? OTA_CLIENT_BLOCK_TIMEOUT_MS
                                : OTA_CLIENT_RSP_TIMEOUT_MS;
        /* 0x34 会隐式擦除整个目标槽（≈40 页 × 25ms ≈ 1s 以上，随固件大小变化），
         * 实测已超过 1s 默认窗口 → 对端完全正常，客户端却误判超时并重试
         * （重试又撞上正在进行的擦除，表现为"永远停在 step 3"）。
         * 故 0x34 单独用 6 倍窗口。 */
        if (s_state == OTA_CS_DOWNLOAD)
        {
          rspTimeoutMs = OTA_CLIENT_DL_TIMEOUT_MS;
        }
      if (s_rspPending)
      {
        if (s_elapsed(s_sentTick, rspTimeoutMs))
        {
          /* ★ 传输态同样走"单块重发"，不能直接判整轮失败 ——
           *   否则前面 78 块全白传（实测走到第 79 块才被这条判死）。
           *   对端在块失败后 s_expectedSeq 未推进，重发同一 seq 可直接接受。 */
          if ((s_state == OTA_CS_TRANSFER) && (s_xferRetry < OTA_CLIENT_XFER_RETRY_MAX))
          {
            s_xferRetry++;
            s_rspPending = false;
            BSP_UART_Printf("[ota] block retry %u/%u off=%lu seq=%u\r\n",
                            (unsigned)s_xferRetry, (unsigned)OTA_CLIENT_XFER_RETRY_MAX,
                            (unsigned long)s_off, (unsigned)UDS_BLOCK_SEQ_OF(s_seq));
          }
          else
          {
            BSP_UART_Printf("[ota] no rsp in step %u (%lums), give up\r\n",
                            (unsigned)s_state, (unsigned long)rspTimeoutMs);
            s_rspPending = false;
            s_go(OTA_CS_FAIL);              /* 退回整体重来 */
          }
        }
        break;                                 /* 等应答中，不重复发 */
      }

      switch (s_state)
      {
        case OTA_CS_PROBE:
        {
          static const uint8_t req[] = {UDS_SID_TESTER_PRESENT, 0x00U};
          s_sendReq(req, sizeof(req));
          break;
        }

case OTA_CS_ENTER_SESSION:
    {
          uint8_t req[2];
          req[0] = UDS_SID_DIAG_SESSION;
          req[1] = UDS_SESSION_EXTENDED;
          s_sendReq(req, sizeof(req));
          break;
        }

        case OTA_CS_QUERY_SLOT:
        {
          /* 22 F1 81 -> ReadDataByIdentifier(ACTIVE_SLOT) */
          uint8_t req[3];
          req[0] = UDS_SID_READ_DID;
          req[1] = (uint8_t)(UDS_DID_ACTIVE_SLOT >> 8);
          req[2] = (uint8_t)(UDS_DID_ACTIVE_SLOT & 0xFFU);
          s_sendReq(req, sizeof(req));
          break;
        }

        case OTA_CS_DOWNLOAD:
        {
          /* 34 00 44 + addr(4B BE) + size(4B BE) = 11 字节 */
          uint8_t req[UDS_DL_REQ_LEN];
          req[0] = UDS_SID_REQUEST_DOWNLOAD;
          req[1] = 0x00U;
          req[2] = 0x44U;
          /* ★ addr 语义 = 目标槽的 APP 链接基址 = 槽基址 + 32B 镜像头（ota/docs/03 §145）。
           *   之前误发 OTA_SLOT_B_ADDR（槽基址），落到 agent 的区间校验之外，
           *   被回 NRC 0x31 requestOutOfRange。
           * ★ size 语义 = **纯 payload 字节数**（不含 32B 头）。
           *   头由设备在 FF02 激活时自己合成（docs/03 §146-148），
           *   上位机不得把镜像头塞进 0x36 数据流。 */
          req[3] = (uint8_t)((s_targetAppAddr >> 24) & 0xFFU);
          req[4] = (uint8_t)((s_targetAppAddr >> 16) & 0xFFU);
          req[5] = (uint8_t)((s_targetAppAddr >>  8) & 0xFFU);
          req[6] = (uint8_t)( s_targetAppAddr         & 0xFFU);
          req[7] = (uint8_t)((s_payloadLen >> 24) & 0xFFU);
          req[8] = (uint8_t)((s_payloadLen >> 16) & 0xFFU);
          req[9] = (uint8_t)((s_payloadLen >>  8) & 0xFFU);
          req[10]= (uint8_t)( s_payloadLen         & 0xFFU);
          /* ★ 只打印真正发出的请求原文（取证用），且重度节流。
       *   取证期信息完整性优先于观感；一旦流程跑通就删掉这整段。 */
          {
            static uint32_t s_dlPrintCnt;
            if ((s_dlPrintCnt++ % 200U) == 0U)
            {
              BSP_UART_Printf("[ota] 0x34 req:");
              for (uint16_t i = 0U; i < sizeof(req); i++)
              { BSP_UART_Printf(" %02X", req[i]); }
              BSP_UART_Printf(" slot=%u len=%u\r\n",
                              (unsigned)s_targetSlot, (unsigned)sizeof(req));
            }
          }
          s_sendReq(req, sizeof(req));
          break;
        }

        /* 0x36 请求缓冲（2B UDS 头 + 块净荷）。static 而非栈上：
 * OTA_CLIENT_CHUNK 最大 1008 字节，放栈上会挤掉主循环的栈空间。 */
static uint8_t          s_xferReq[2U + OTA_CLIENT_CHUNK];

        case OTA_CS_TRANSFER:
        {
          /* 36 + seq + chunk。★ 跳过 32B 镜像头，只传纯 payload（docs/03 §146） */
          uint8_t  *req = s_xferReq;
          uint16_t n = OTA_CLIENT_CHUNK;

          if ((s_off + n) > s_payloadLen)
          {
            n = (uint16_t)(s_payloadLen - s_off);
          }

          req[0] = UDS_SID_TRANSFER_DATA;
          req[1] = UDS_BLOCK_SEQ_OF(s_seq);
          memcpy(&req[2], &g_otaImage[OTA_PAYLOAD_OFF + s_off], n);
          g_otaCliBlockSeq = req[1];
          s_sendReq(req, (uint16_t)(2U + n));
          break;
        }

        case OTA_CS_EXIT:
        {
          /* 37 + payload CRC32(4B BE)。该 CRC 同时充当 ①FF01 参考值
           * ②设备合成镜像头里的 payloadCrc（docs/03 §152）。 */
          uint8_t req[5];
          req[0] = UDS_SID_TRANSFER_EXIT;
          req[1] = (uint8_t)(s_payloadCrc >> 24);
          req[2] = (uint8_t)(s_payloadCrc >> 16);
          req[3] = (uint8_t)(s_payloadCrc >>  8);
          req[4] = (uint8_t)( s_payloadCrc);
          s_sendReq(req, sizeof(req));
          break;
        }

        case OTA_CS_RESET:
        {
          /* 11 01：硬复位目标板，使其从新槽试运行 */
          uint8_t req[2] = {UDS_SID_ECU_RESET, 0x01U};
          s_sendReq(req, sizeof(req));
          break;
        }

        case OTA_CS_VERIFY:
        {
          /* 31 01 FF01：CRC32 校验 */
          uint8_t req[4] = {UDS_SID_ROUTINE_CTRL, 0x01U, 0xFFU, 0x01U};
          s_sendReq(req, sizeof(req));
          break;
        }

        case OTA_CS_ACTIVATE:
        {
          /* 31 01 FF02：激活目标槽（设备在此合成槽首 32B 镜像头） */
          uint8_t req[4] = {UDS_SID_ROUTINE_CTRL, 0x01U, 0xFFU, 0x02U};
          s_sendReq(req, sizeof(req));
          break;
        }

        default:
          break;
      }
      break;
      }   /* rspTimeoutMs 作用域结束 */

    default:
      break;
  }
}

void OtaClient_Start(void)
{
  s_retryCnt = 0U;
  s_startSequence();
}

void OtaClient_GetStats(uint32_t *req, uint32_t *rsp, uint32_t *nrc, uint8_t *state)
{
  if (req != NULL) { *req  = g_otaCliReqCnt; }
  if (rsp != NULL) { *rsp  = g_otaCliRspCnt; }
  if (nrc != NULL) { *nrc  = g_otaCliNrcCnt; }
  if (state != NULL){ *state = g_otaCliState; }
}

#else  /* OTA_CLIENT_ENABLE == 0 */

/* 占位：保持链接完整但什么都不做 */
void OtaClient_Task(void)            { }
void OtaClient_Start(void)           { }
void OtaClient_GetStats(uint32_t *req, uint32_t *rsp, uint32_t *nrc, uint8_t *state)
{
  if (req != NULL)  { *req  = 0U; }
  if (rsp != NULL)  { *rsp  = 0U; }
  if (nrc != NULL)  { *nrc  = 0U; }
  if (state != NULL){ *state = 0U; }
}
void OtaClient_OnResp(const uint8_t *rsp, uint16_t len)
{
  (void)rsp; (void)len;
}
void OtaClient_OnIsoTpErr(uint8_t err)
{
  (void)err;
}

#endif /* OTA_CLIENT_ENABLE */