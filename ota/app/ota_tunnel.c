/**
  ******************************************************************************
  * @file    ota_tunnel.c
  * @brief   串口 ↔ 诊断CAN 隧道实现（详见 ota_tunnel.h 的行协议说明）
  *
  * 实现要点：
  *   1. 解析器手写、不依赖 strtok 的可重入性（Console_Execute 已用过 strtok，
  *      再调用会破坏其内部状态，因此这里自己扫字符串）；
  *   2. 打印开销按"每帧一行"计，115200 下一行 8 字节帧约 26 字符 ≈ 2.3ms，
  *      对 500kbit/s 下的 ISO-TP 流（每 2ms 一帧）是临界负载 —— 因此隧道模式下
  *      上位机必须按 ISO-TP 单事务半双工严格一问一答，不要并发灌帧；
  *   3. 只转发诊断 ID，业务帧 0x321/0x322 不占隧道带宽。
  ******************************************************************************
  */
#include "ota_tunnel.h"
#include "can.h"
#include "bsp_uart.h"
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

static bool s_tunnelOn;

void OtaTunnel_Init(void)
{
  s_tunnelOn = false;
}

bool OtaTunnel_IsOn(void)
{
  return s_tunnelOn;
}

/*--------------------------------------- 小工具 ----------------------------------------*/
static int8_t HexNibble(char c)
{
  if ((c >= '0') && (c <= '9')) { return (int8_t)(c - '0'); }
  if ((c >= 'a') && (c <= 'f')) { return (int8_t)(c - 'a' + 10); }
  if ((c >= 'A') && (c <= 'F')) { return (int8_t)(c - 'A' + 10); }
  return -1;
}

/**
  * @brief  连续解析十六进制字节（以非十六进制字符分隔）
  * @retval 解析到的字节数；-1 表示遇到非法十六进制字符
  */
static int16_t ParseHexBytes(const char *p, uint8_t *out, uint16_t outMax)
{
  uint16_t n = 0U;

  if (p == NULL)
  {
    return 0;
  }

  while (*p != '\0')
  {
    int8_t hi = HexNibble(*p);
    if (hi < 0)
    {
      p++;
      continue;
    }
    p++;
    if (*p == '\0')
    {
      return -1;                                   /* 奇数个十六进制字符 */
    }
    {
      int8_t lo = HexNibble(*p);
      if (lo < 0)
      {
        return -1;
      }
      p++;
      if (n >= outMax)
      {
        return -1;                                 /* 超出目标缓冲 */
      }
      out[n++] = (uint8_t)(((uint8_t)hi << 4) | (uint8_t)lo);
    }
  }
  return (int16_t)n;
}

static void PrintFrameLine(const char *tag, uint32_t id, const uint8_t *d, uint8_t len)
{
  char line[48];
  int  n = 0;

  n += (int)snprintf(&line[n], sizeof(line) - (size_t)n, "%s %03lX %u", tag,
                     (unsigned long)id, (unsigned)len);
  for (uint8_t i = 0U; i < len; i++)
  {
    n += (int)snprintf(&line[n], sizeof(line) - (size_t)n, " %02X", d[i]);
  }
  n += (int)snprintf(&line[n], sizeof(line) - (size_t)n, "\r\n");
  if (n > (int)sizeof(line))
  {
    n = (int)sizeof(line);
  }
  BSP_UART_Send(line, (uint16_t)n);
}

/*--------------------------------------- 命令处理 --------------------------------------*/
static void Cmd_Tun(const char *args)
{
  if (args == NULL)
  {
    BSP_UART_Send("usage: tun <on|off>\r\n", 21U);
    return;
  }
  if (strcmp(args, "on") == 0)
  {
    s_tunnelOn = true;
    BSP_UART_Send("tunnel: on\r\n", 12U);
  }
  else if (strcmp(args, "off") == 0)
  {
    s_tunnelOn = false;
    BSP_UART_Send("tunnel: off\r\n", 13U);
  }
  else
  {
    BSP_UART_Send("usage: tun <on|off>\r\n", 21U);
  }
}

/**
  * @brief  tx <id3hex> <n> <2n个hex>：把一帧原始 CAN 帧发到总线
  */
static void Cmd_Tx(const char *args)
{
  char     idbuf[8];
  uint32_t id = 0U;
  uint32_t want;
  uint8_t  data[CAN_PAYLOAD_LEN + 1U];
  uint16_t i = 0U;
  int16_t  got;
  const char *p = args;

  if (p == NULL)
  {
    BSP_UART_Send("TXERR\r\n", 7U);
    return;
  }

  /* 1. 解析 3 位十六进制 ID */
  while ((*p == ' ') || (*p == '\t')) { p++; }
  while ((*p != '\0') && (*p != ' ') && (i < (sizeof(idbuf) - 1U)))
  {
    idbuf[i++] = *p++;
  }
  idbuf[i] = '\0';
  {
    int8_t nib;
    for (uint16_t k = 0U; k < i; k++)
    {
      nib = HexNibble(idbuf[k]);
      if (nib < 0)
      {
        BSP_UART_Send("TXERR\r\n", 7U);
        return;
      }
      id = (id << 4) | (uint32_t)nib;
    }
  }
  if ((i == 0U) || (id > 0x7FFU))
  {
    BSP_UART_Send("TXERR\r\n", 7U);
    return;
  }

  /* 2. 解析期望长度 n */
  while ((*p == ' ') || (*p == '\t')) { p++; }
  want = 0U;
  i = 0U;
  while ((*p >= '0') && (*p <= '9') && (i < 4U))
  {
    want = (want * 10U) + (uint32_t)(*p - '0');
    p++;
    i++;
  }
  if ((i == 0U) || (want > CAN_PAYLOAD_LEN))
  {
    BSP_UART_Send("TXERR\r\n", 7U);
    return;
  }

  /* 3. 解析 n 个数据字节 */
  got = ParseHexBytes(p, data, CAN_PAYLOAD_LEN);
  if ((got < 0) || ((uint32_t)got != want))
  {
    BSP_UART_Send("TXERR\r\n", 7U);
    return;
  }

  /* 4. 发出 */
  if (CAN_Send(id, data, (uint8_t)want) != HAL_OK)
  {
    BSP_UART_Send("TXERR\r\n", 7U);
    return;
  }
  BSP_UART_Send("TXOK\r\n", 6U);
}

bool OtaTunnel_OnLine(const char *cmd, const char *args)
{
  if (cmd == NULL)
  {
    return false;
  }
  if (strcmp(cmd, "tun") == 0)
  {
    Cmd_Tun(args);
    return true;
  }
  if (strcmp(cmd, "tx") == 0)
  {
    Cmd_Tx(args);
    return true;
  }
  return false;
}

/*--------------------------------------- 收帧回吐 --------------------------------------*/
void OtaTunnel_PrintFrame(const CAN_RxMsg *msg)
{
  if ((!s_tunnelOn) || (msg == NULL))
  {
    return;
  }

  /* 只转发诊断 ID（含两个方向）：7E0 是上位机自己发出的请求（回显，主机自行忽略），
   * 7E8 是目标节点的应答与流控帧 */
  if ((msg->id >= OTA_TUNNEL_DIAG_ID_LO) && (msg->id <= OTA_TUNNEL_DIAG_ID_HI))
  {
    PrintFrameLine("RXTX", msg->id, msg->data, msg->len);
  }
}
