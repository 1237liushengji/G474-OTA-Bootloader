/**
  ******************************************************************************
  * @file    uds.c
  * @brief   UDS（ISO 14229）精简诊断栈实现
  *
  * 服务器（板B，ECU角色）：
  *   请求经ISO-TP到达 Uds_ServerOnMsg -> 服务分发 -> 应答经ISO-TP回传。
  *   会话管理：0x10进入扩展会话后，S3(5s)内无新请求/TesterPresent则回默认会话；
  *   0x2E写操作仅扩展会话允许（默认会话回NRC 0x22）——演示UDS安全语义。
  *   DTC合成：把驱动的真实错误计数映射为故障码（有记录则状态位=1）。
  *
  * 客户端（板A，诊断仪角色）：
  *   Uds_ClientRequest发送；应答回来经Uds_ClientOnMsg打印；1s无应答报超时。
  ******************************************************************************
  */
#include "uds.h"
#include "isotp.h"
#include "can.h"
#include "bsp_uart.h"
#include "ota_agent.h"   /* OTA：0x34/0x36/0x37/0x31/0x11 与 F180~F190 */
#include "ota_client.h"  /* 【2026-10-12】板A 板内 OTA 客户端（状态机） */
#include <string.h>

/*--------------------------------------- 模块状态 --------------------------------------*/
static uint8_t  s_session;                       /* 当前会话（服务器） */
static uint32_t s_lastActivityTick;              /* S3计时基准 */

static uint32_t s_pendingTick;                   /* 客户端：已发请求等待应答的时刻 */
static bool     s_pendingResp;

static bool     s_clientExtended;                /* 客户端镜像：服务器处于扩展会话 */
static uint32_t s_lastTpTick;                    /* 上次TesterPresent时刻 */

/*--------------------------------------- 内部工具 --------------------------------------*/
static void Uds_SendResp(const uint8_t *resp, uint16_t len)
{
  (void)IsoTp_Send(resp, len);                   /* 忙则丢弃（客户端会超时重试） */
}

static void Uds_SendNrc(uint8_t sid, uint8_t nrc)
{
  uint8_t resp[3];
  resp[0] = 0x7FU;
  resp[1] = sid;
  resp[2] = nrc;
  Uds_SendResp(resp, 3U);
}

/**
  * @brief  0x22 ReadDataByIdentifier
  */
static void Uds_Service22(const uint8_t *req, uint16_t len)
{
  /* OTA 扩展：ISOTP_MAX_MSG 已提升到 1024，应答缓冲必须同步放大，
   * 否则一旦某个 DID 的应答超过 64 字节，rlen 会越界写栈（静默破坏内存）。 */
  uint8_t  resp[ISOTP_MAX_MSG];
  uint16_t rlen = 0U;
  uint16_t did;
  const CAN_Stats *st;

  if (len != 3U)                                 /* 精简版：单DID请求 */
  {
    Uds_SendNrc(0x22U, 0x31U);
    return;
  }

  did = (uint16_t)(((uint16_t)req[1] << 8) | req[2]);
  resp[rlen++] = 0x62U;
  resp[rlen++] = (uint8_t)(did >> 8);
  resp[rlen++] = (uint8_t)(did & 0xFFU);

  switch (did)
  {
    case UDS_DID_VERSION:
    {
      const char ver[] = "G474-CAN v3.0";
      for (uint16_t i = 0U; i < sizeof(ver) - 1U; i++)
      {
        resp[rlen++] = (uint8_t)ver[i];
      }
      break;                                     /* 16字节应答->多帧，验证ISO-TP */
    }
    case UDS_DID_UPTIME:
    {
      uint32_t up = HAL_GetTick() / 1000U;
      resp[rlen++] = (uint8_t)(up >> 24);
      resp[rlen++] = (uint8_t)(up >> 16);
      resp[rlen++] = (uint8_t)(up >> 8);
      resp[rlen++] = (uint8_t)up;
      break;
    }
    case UDS_DID_STATS:
    {
      st = CAN_GetStats();
      uint32_t fields[4];
      fields[0] = st->txCount;
      fields[1] = st->rxCount;
      fields[2] = st->txAckCount;
      fields[3] = st->txErrors + st->rxOverruns + st->busOffCount +
                  st->errPassiveCount + st->protocolErrors;
      for (uint8_t i = 0U; i < 4U; i++)
      {
        resp[rlen++] = (uint8_t)(fields[i] >> 24);
        resp[rlen++] = (uint8_t)(fields[i] >> 16);
        resp[rlen++] = (uint8_t)(fields[i] >> 8);
        resp[rlen++] = (uint8_t)fields[i];
      }
      break;
    }
    case UDS_DID_LED:
      resp[rlen++] = App_GetLed();
      break;
    default:
    {
      /* OTA 只读 DID（F180 状态 / F181 活动槽 / F182 版本 / F183 槽CRC / F184 能力位图）：
       * 交 OTA 模块填充；返回 0 表示该 DID 不归 OTA 管，按未知 DID 处理。 */
      uint16_t n = Ota_ReadDid(did, &resp[3], (uint16_t)(sizeof(resp) - 3U));
      if (n == 0U)
      {
        Uds_SendNrc(0x22U, 0x31U);
        return;
      }
      rlen = n;                       /* Ota_ReadDid 已含 62 <DID> 头 */
      break;
    }
  }

  Uds_SendResp(resp, rlen);
}

/**
  * @brief  0x2E WriteDataByIdentifier（仅扩展会话）
  */
static void Uds_Service2E(const uint8_t *req, uint16_t len)
{
  if (s_session != UDS_SESSION_EXTENDED)
  {
    Uds_SendNrc(0x2EU, 0x22U);                   /* conditionsNotCorrect */
    return;
  }
  /* 长度守卫：DID 在 req[1..2]，长度不足 3 时不得读取（否则越界读） */
  if (len < 3U)
  {
    Uds_SendNrc(0x2EU, 0x13U);            /* incorrectMessageLengthOrInvalidFormat */
    return;
  }

  /* OTA 扩展：F190 写 01 = 置"下次复位进 Bootloader"请求；长度由 Ota_OnWriteBootReq 校验 */
  if ((uint16_t)(((uint16_t)req[1] << 8) | req[2]) == UDS_DID_BOOT_REQ)
  {
    Ota_OnWriteBootReq(req, len);
    return;
  }

  if ((len != 4U) || ((uint16_t)(((uint16_t)req[1] << 8) | req[2]) != UDS_DID_LED))
  {
    Uds_SendNrc(0x2EU, 0x31U);
    return;
  }

  App_SetLed(req[3]);

  uint8_t resp[3];
  resp[0] = 0x6EU;
  resp[1] = req[1];
  resp[2] = req[2];
  Uds_SendResp(resp, 3U);
}

/**
  * @brief  0x19 ReadDTCInformation（子功能01按状态掩码）
  * @note   DTC由真实事件合成：状态位=0x01（当前故障，计数>0时置位）
  */
static void Uds_Service19(const uint8_t *req, uint16_t len)
{
  const CAN_Stats *st = CAN_GetStats();
  uint8_t resp[16];
  uint16_t rlen = 0U;
  uint8_t statusAvail = 0x01U;                   /* 支持的状态位 */
  struct { uint32_t dtc; uint8_t active; } dtcs[3];

  dtcs[0].dtc = 0xC100U; dtcs[0].active = (st->busOffCount > 0U) ? 1U : 0U;
  dtcs[1].dtc = 0xC101U; dtcs[1].active = (st->errPassiveCount > 0U) ? 1U : 0U;
  dtcs[2].dtc = 0xC102U; dtcs[2].active = ((st->protocolErrors + st->txEvtLost) > 0U) ? 1U : 0U;

  if ((len != 3U) || (req[1] != 0x01U))
  {
    Uds_SendNrc(0x19U, 0x31U);
    return;
  }

  resp[rlen++] = 0x59U;
  resp[rlen++] = 0x01U;
  resp[rlen++] = statusAvail;
  for (uint8_t i = 0U; i < 3U; i++)
  {
    resp[rlen++] = (uint8_t)(dtcs[i].dtc >> 8);
    resp[rlen++] = (uint8_t)(dtcs[i].dtc & 0xFFU);
    resp[rlen++] = dtcs[i].active;
  }
  Uds_SendResp(resp, rlen);
}

/**
  * @brief  0x10 DiagnosticSessionControl
  */
static void Uds_Service10(const uint8_t *req, uint16_t len)
{
  uint8_t sub;

  if (len != 2U)
  {
    Uds_SendNrc(0x10U, 0x31U);
    return;
  }

  sub = req[1] & 0x7FU;
  if ((sub != UDS_SESSION_DEFAULT) && (sub != UDS_SESSION_EXTENDED))
  {
    Uds_SendNrc(0x10U, 0x12U);                   /* subFunctionNotSupported */
    return;
  }

  s_session = sub;
  /* 应答：50 <sub> P2(50ms) P2*(5000ms 10ms单位) */
  uint8_t resp[6];
  resp[0] = 0x50U;
  resp[1] = sub;
  resp[2] = 0x00U;
  resp[3] = 0x32U;
  resp[4] = 0x01U;
  resp[5] = 0xF4U;
  Uds_SendResp(resp, 6U);
}

/*--------------------------------------- 服务器 --------------------------------------*/
/**
  * @brief  服务器：处理一条诊断请求（经ISO-TP到达）
  */
void Uds_ServerOnMsg(const uint8_t *req, uint16_t len)
{
  if ((len < 1U) || (len > ISOTP_MAX_MSG))
  {
    return;
  }

  s_lastActivityTick = HAL_GetTick();            /* 任何有效请求刷新S3 */

  switch (req[0])
  {
    case 0x10U: Uds_Service10(req, len); break;
    case 0x22U: Uds_Service22(req, len); break;
    case 0x2EU: Uds_Service2E(req, len); break;
    case 0x19U: Uds_Service19(req, len); break;
    /* ---- OTA 刷写服务（会话约束在各 handler 内部检查）---- */
    case 0x11U: Ota_OnEcuReset(req, len);        break;   /* ECUReset      */
    case 0x31U: Ota_OnRoutineControl(req, len);  break;   /* RoutineControl*/
    case 0x34U: Ota_OnRequestDownload(req, len); break;   /* RequestDownload*/
    case 0x36U: Ota_OnTransferData(req, len);    break;   /* TransferData  */
    case 0x37U: Ota_OnTransferExit(req, len);    break;   /* TransferExit  */
    case 0x3EU:                                   /* TesterPresent */
      if ((len == 2U) && ((req[1] & 0x7FU) == 0x00U))
      {
        if ((req[1] & 0x80U) == 0U)               /* 非 SuppressPosRsp */
        {
          uint8_t resp[2] = {0x7EU, 0x00U};
          Uds_SendResp(resp, 2U);
        }
      }
      else
      {
        Uds_SendNrc(0x3EU, 0x12U);
      }
      break;
    default:
      Uds_SendNrc(req[0], 0x11U);                /* serviceNotSupported */
      break;
  }
}

/*--------------------------------------- 客户端 --------------------------------------*/
static void Uds_ClientErr(IsoTp_Result result)
{
  const char *msg;
  switch (result)
  {
    case ISOTP_TIMEOUT:     msg = "diag: isotp timeout\r\n"; break;
    case ISOTP_SN_ERROR:    msg = "diag: isotp SN error\r\n"; break;
    case ISOTP_OVERFLOW:    msg = "diag: isotp overflow\r\n"; break;
    default:                msg = "diag: isotp error\r\n"; break;
  }
  BSP_UART_Send(msg, (uint16_t)strlen(msg));
  s_pendingResp = false;

  /* 【2026-10-12】转给板内 OTA 客户端：ISO-TP 层的失败必须把状态机踢出
   * "等应答"僵局，否则 s_rspPending 永远为 true，客户端既不推进也不超时。 */
  OtaClient_OnIsoTpErr((uint8_t)result);
}

/**
  * @brief  客户端：发起一次诊断请求
  */
IsoTp_Result Uds_ClientRequest(const uint8_t *req, uint16_t len)
{
  IsoTp_Result r = IsoTp_Send(req, len);
  if (r == ISOTP_OK)
  {
    s_pendingResp  = true;
    s_pendingTick  = HAL_GetTick();
  }
  return r;
}

/**
  * @brief  客户端：收到诊断应答（经ISO-TP拼装完整）
  */
void Uds_ClientOnMsg(const uint8_t *rsp, uint16_t len)
{
  s_pendingResp = false;

  /* 【2026-10-12】板A 板内 OTA 客户端：把完整应答交给状态机推进。
   * 必须在下面的人读解码之前调用（状态机要靠 SID 回推进）。 */
  OtaClient_OnResp(rsp, len);

  /* 会话镜像：0x10的正/负应答更新客户端会话认知 */
  if ((len == 6U) && (rsp[0] == 0x50U))
  {
    s_clientExtended = (rsp[1] == UDS_SESSION_EXTENDED);
    s_lastTpTick     = HAL_GetTick();
  }
  else if ((len == 3U) && (rsp[0] == 0x7FU) && (rsp[1] == 0x10U))
  {
    s_clientExtended = false;
  }
  else
  {
    /* 其他应答 */
  }

  /*【2026-10-12降噪】OTA 全量传输会产生数千条 0x36 应答，逐条打印会把
   * 115200 波特率的串口刷满，真正的 [ota] 阶段日志被挤出缓冲、抓不到。
   * 故每 200 条打印一次采样；OTA 客户端自己会打关键节点日志，不受影响。 */
  {
    static uint32_t s_diagLogCnt;
    s_diagLogCnt++;
    if ((s_diagLogCnt % 200U) != 1U)
    {
      return;                                     /* 跳过本次的解码与打印 */
    }
    BSP_UART_Printf("[diag] #%u ", (unsigned)s_diagLogCnt);
  }

  BSP_UART_Printf("diag<< (%u bytes):", (unsigned)len);
  for (uint16_t i = 0U; i < len; i++)
  {
    BSP_UART_Printf(" %02X", rsp[i]);
  }
  BSP_UART_Send("\r\n", 2U);

  /* 友好解码常见应答 */
  if (len >= 3U)
  {
    if (rsp[0] == 0x62U)
    {
      uint16_t did = (uint16_t)(((uint16_t)rsp[1] << 8) | rsp[2]);
      if ((did == UDS_DID_VERSION) && (len > 3U))
      {
        BSP_UART_Send("  ver: ", 7U);
        BSP_UART_Send((const char *)&rsp[3], (uint16_t)(len - 3U));
        BSP_UART_Send("\r\n", 2U);
      }
      else if ((did == UDS_DID_UPTIME) && (len == 7U))
      {
        uint32_t up = ((uint32_t)rsp[3] << 24) | ((uint32_t)rsp[4] << 16) |
                      ((uint32_t)rsp[5] << 8) | rsp[6];
        BSP_UART_Printf("  uptime: %lus\r\n", (unsigned long)up);
      }
      else if ((did == UDS_DID_LED) && (len == 4U))
      {
        BSP_UART_Printf("  led: %u\r\n", (unsigned)rsp[3]);
      }
    }
    else if (rsp[0] == 0x7FU)
    {
      BSP_UART_Printf("  NRC: sid=%02X code=%02X\r\n", rsp[1], rsp[2]);
    }
    else if (rsp[0] == 0x59U)
    {
      BSP_UART_Send("  DTC list (id,active):\r\n", 24U);
      for (uint16_t i = 3U; (i + 2U) < len; i += 3U)
      {
        BSP_UART_Printf("   %02X%02X %u\r\n", rsp[i], rsp[i + 1U], rsp[i + 2U]);
      }
    }
  }
}

/*--------------------------------------- 初始化与任务 --------------------------------------*/
/**
  * @brief  按节点角色初始化：A=诊断仪(客户端)，B=ECU(服务器)
  */
void Uds_Init(void)
{
  s_session = UDS_SESSION_DEFAULT;
  s_lastActivityTick = HAL_GetTick();
  s_pendingResp = false;
  s_clientExtended = false;
  s_lastTpTick = HAL_GetTick();

#if (CAN_NODE_ROLE == CAN_NODE_A)
  IsoTp_Init(ISOTP_DIAG_RESP_ID, ISOTP_DIAG_REQ_ID,
             Uds_ClientOnMsg, Uds_ClientErr);
#else
  IsoTp_Init(ISOTP_DIAG_REQ_ID, ISOTP_DIAG_RESP_ID,
             Uds_ServerOnMsg, NULL);
#endif
}

/**
  * @brief  主循环轮询：S3会话回退 + 客户端应答超时
  */
void Uds_Task(void)
{
  uint32_t now = HAL_GetTick();

  /* 服务器：扩展会话S3超时回默认 */
  if ((s_session == UDS_SESSION_EXTENDED) &&
      ((now - s_lastActivityTick) > UDS_S3_TIMEOUT_MS))
  {
    s_session = UDS_SESSION_DEFAULT;
  }

  /* 客户端：1s无应答报超时 */
  if (s_pendingResp && ((now - s_pendingTick) > ISOTP_TIMEOUT_MS))
  {
    s_pendingResp = false;
    BSP_UART_Send("diag: response timeout\r\n", 25U);
  }

  /* 客户端：扩展会话期间每2s发TesterPresent(3E 80抑制正应答)保活S3，
   * 与真实诊断仪（CANoe/PCAN-Diag）的做法一致；不经pending流程（无应答为正常） */
  if (s_clientExtended && ((now - s_lastTpTick) >= 2000U))
  {
    uint8_t tp[2] = {0x3EU, 0x80U};
    s_lastTpTick = now;
    (void)IsoTp_Send(tp, 2U);
  }
}
