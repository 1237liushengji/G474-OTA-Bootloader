/**
  ******************************************************************************
  * @file    usb_cdc.c
  * @brief   MCU 原生 USB 虚拟串口（CDC-ACM）粘合层
  *          类接口回调 + 双向环形缓冲 + 对外 API
  *
  * 线程/中断模型（本工程为裸机 + 中断，无 RTOS）：
  *   - USB 中断（USB_LP_IRQHandler → HAL_PCD_IRQHandler → 库回调）里只做
  *     **搬运**：收到的数据拷进 RX 环、更新计数。绝不在中断里做耗时操作。
  *   - 主循环通过 UsbCdc_Task() 把 TX 环的数据搬到 IN 端点。
  *   - 环形缓冲使用单生产者/单消费者 + volatile 头尾指针，无需临界区
  *     （读写指针各自只被一方修改）。
  ******************************************************************************
  */
#include "usb_cdc.h"
#include "bsp_uart.h"      /* 仅为 step 打点 */
#include "usbd_core.h"
#include "usbd_cdc.h"
#include "usbd_desc.h"

/*------------------------------------------------------------ 缓冲尺寸 -----*/
#define CDC_RX_RING_SIZE     512U
#define CDC_TX_RING_SIZE     2048U
#define CDC_DATA_MPS         64U
#define CDC_NOTIFY_MPS       8U

/*------------------------------------------------------------ 内部状态 -----*/
USBD_HandleTypeDef hUsbDeviceFS;

static uint8_t  s_rxRing[CDC_RX_RING_SIZE];
static volatile uint16_t s_rxHead;
static volatile uint16_t s_rxTail;

static uint8_t  s_txRing[CDC_TX_RING_SIZE];
static volatile uint16_t s_txHead;
static volatile uint16_t s_txTail;

static uint8_t  s_cdcTxBuf[CDC_DATA_MPS];        /* 单次批量发送缓冲 */
static uint8_t  s_cdcRxBuf[CDC_DATA_MPS];        /* 单次批量接收缓冲 */

static volatile uint32_t s_resetCnt;
static volatile uint32_t s_rxBytes;
static volatile uint32_t s_txBytes;
static volatile uint32_t s_txDropBytes;

static uint8_t  s_ready;                          /* 1 = 已 set_configuration */

/*------------------------------------------------------ CDC 类接口回调 -----*/
static int8_t  Cdc_Init(void);
static int8_t  Cdc_DeInit(void);
static int8_t  Cdc_Control(uint8_t cmd, uint8_t *pbuf, uint16_t length);
static int8_t  Cdc_Receive(uint8_t *buf, uint32_t *len);

static USBD_CDC_ItfTypeDef s_cdcFops =
{
  Cdc_Init,
  Cdc_DeInit,
  Cdc_Control,
  Cdc_Receive,
};

static int8_t Cdc_Init(void)
{
  /* ⚠️【关键时序】本回调由 USBD_CDC_Init() 在**分配 pClassData 之后**调用，
   * 而紧接着 USBD_CDC_Init() 就会用 hcdc->RxBuffer 去挂载 OUT 端点接收。
   * 因此收/发缓冲**必须在这里**设置。
   *
   * 踩过的坑：ST 库的 USBD_CDC_SetTxBuffer()/SetRxBuffer() **没有空指针检查**
   *   （直接 hcdc->TxBuffer = pbuff，hcdc = pdev->pClassData）。
   *   若在 UsbCdc_Init() 里（枚举前，pClassData 仍为 NULL）就调用它们，
   *   会往地址 0x000000xx 写数据 → 触发 MemManage/Bus 故障 → 板子重启循环。
   *   实测现象：APP 刚启动就打印 "!! MEMMANAGE FAULT !!"。
   *   正确做法就是：缓冲设置只放在本回调里（枚举成功、pClassData 已就绪）。 */
  (void)USBD_CDC_SetTxBuffer(&hUsbDeviceFS, s_cdcTxBuf, 0U);
  (void)USBD_CDC_SetRxBuffer(&hUsbDeviceFS, s_cdcRxBuf);

  return (int8_t)USBD_OK;
}

static int8_t Cdc_DeInit(void)
{
  return (int8_t)USBD_OK;
}

static int8_t Cdc_Control(uint8_t cmd, uint8_t *pbuf, uint16_t length)
{
  /* CDC 类请求（SET/GET_LINE_CODING、SET_CONTROL_LINE_STATE 等）。
   * 波特率对我们无意义（USB 批量传输与波特率无关），全部接受即可。
   * 对 SET_LINE_CODING（0x20）与 SET_CONTROL_LINE_STATE（0x22）直接返回 OK，
   * 否则主机可能认为设备不支持而反复重试。 */
  (void)cmd;
  (void)pbuf;
  (void)length;
  return (int8_t)USBD_OK;
}

static int8_t Cdc_Receive(uint8_t *buf, uint32_t *len)
{
  uint32_t n = *len;
  uint32_t i;

  /* 中断上下文：只做搬运 */
  for (i = 0U; i < n; i++)
  {
    uint16_t next = (uint16_t)((s_rxHead + 1U) % CDC_RX_RING_SIZE);

    if (next != s_rxTail)
    {
      s_rxRing[s_rxHead] = buf[i];
      s_rxHead = next;
      s_rxBytes++;
    }
    else
    {
      break;                                     /* 环满：丢弃余下字节（控制台场景可接受） */
    }
  }

  /* 重新挂载下一次接收（ST 标准做法：由应用回调负责续挂） */
  (void)USBD_CDC_ReceivePacket(&hUsbDeviceFS);

  return (int8_t)USBD_OK;
}

/*-------------------------------------------------------------- 对外 API --*/
void UsbCdc_Init(void)
{
  /* 线/时钟/引脚等底层由 usbd_conf.c 的 HAL_PCD_MspInit 负责 */
  BSP_UART_Send("[usb] step1 USBD_Init\r\n", 20U);
  (void)USBD_Init(&hUsbDeviceFS, &CDC_Desc, 0U);

  BSP_UART_Send("[usb] step2 RegisterClass\r\n", 24U);
  (void)USBD_RegisterClass(&hUsbDeviceFS, &USBD_CDC);

  BSP_UART_Send("[usb] step3 RegisterInterface\r\n", 27U);
  (void)USBD_CDC_RegisterInterface(&hUsbDeviceFS, &s_cdcFops);

  BSP_UART_Send("[usb] step4 USBD_Start\r\n", 20U);
  /* ⚠️ 这里**不能**调用 USBD_CDC_SetTxBuffer/SetRxBuffer：
   * 此时 pClassData 还是 NULL（要等主机 SET_CONFIGURATION 时
   * USBD_CDC_Init 才分配），而库函数没有空指针检查，会写地址 0 → 故障。
   * 缓冲区的设置在 Cdc_Init() 回调里完成即可。 */
  (void)USBD_Start(&hUsbDeviceFS);               /* 使能外设 + D+ 上拉，向主机宣告接入 */

  BSP_UART_Send("[usb] step5 done\r\n", 17U);
}

uint8_t UsbCdc_IsConfigured(void)
{
  /* USBD_STATE_CONFIGURED = 3 */
  return (hUsbDeviceFS.dev_state == USBD_STATE_CONFIGURED) ? 1U : 0U;
}

uint8_t UsbCdc_IsConnected(void)
{
  return (hUsbDeviceFS.dev_state != USBD_STATE_DEFAULT) ? 1U : 0U;
}

void UsbCdc_Task(void)
{
  uint16_t avail;
  uint16_t n;
  uint16_t i;

  if (UsbCdc_IsConfigured() == 0U)
  {
    return;
  }

  /* 把 TX 环里的数据搬到 CDC 发送缓冲并启动一次批量 IN。
   * 若上一次传输还没被主机取走，USBD_CDC_TransmitPacket 会返回 USBD_BUSY，
   * 我们直接返回，下一圈再试 —— 全程不阻塞。 */
  avail = (uint16_t)((s_txHead - s_txTail + CDC_TX_RING_SIZE) % CDC_TX_RING_SIZE);
  if (avail == 0U)
  {
    return;
  }

  n = (avail > CDC_DATA_MPS) ? CDC_DATA_MPS : avail;
  for (i = 0U; i < n; i++)
  {
    s_cdcTxBuf[i] = s_txRing[s_txTail];
    s_txTail = (uint16_t)((s_txTail + 1U) % CDC_TX_RING_SIZE);
  }

  (void)USBD_CDC_SetTxBuffer(&hUsbDeviceFS, s_cdcTxBuf, n);
  if (USBD_CDC_TransmitPacket(&hUsbDeviceFS) == (uint8_t)USBD_OK)
  {
    s_txBytes += n;
  }
  else
  {
    /* 端点忙：把字节还回环（简单回退，避免丢日志） */
    s_txTail = (uint16_t)((s_txTail - n + CDC_TX_RING_SIZE) % CDC_TX_RING_SIZE);
  }
}

uint16_t UsbCdc_Available(void)
{
  return (uint16_t)((s_rxHead - s_rxTail + CDC_RX_RING_SIZE) % CDC_RX_RING_SIZE);
}

uint16_t UsbCdc_Read(uint8_t *buf, uint16_t len)
{
  uint16_t avail = UsbCdc_Available();
  uint16_t n = (avail < len) ? avail : len;
  uint16_t i;

  for (i = 0U; i < n; i++)
  {
    buf[i] = s_rxRing[s_rxTail];
    s_rxTail = (uint16_t)((s_rxTail + 1U) % CDC_RX_RING_SIZE);
  }
  return n;
}

int16_t UsbCdc_GetChar(void)
{
  if (s_rxTail == s_rxHead)
  {
    return -1;
  }

  {
    uint8_t c = s_rxRing[s_rxTail];
    s_rxTail = (uint16_t)((s_rxTail + 1U) % CDC_RX_RING_SIZE);
    return (int16_t)c;
  }
}

uint16_t UsbCdc_Write(const uint8_t *buf, uint16_t len)
{
  uint16_t written = 0U;
  uint16_t i;

  if (buf == NULL)
  {
    return 0U;
  }

  for (i = 0U; i < len; i++)
  {
    uint16_t next = (uint16_t)((s_txHead + 1U) % CDC_TX_RING_SIZE);

    if (next == s_txTail)
    {
      s_txDropBytes += (uint32_t)(len - i);
      break;                                     /* 环满：丢弃剩余 */
    }
    s_txRing[s_txHead] = buf[i];
    s_txHead = next;
    written++;
  }

  return written;
}

/*----------------------------------------------------------- 诊断计数 -----*/
uint32_t UsbCdc_ResetCount(void)  { return s_resetCnt; }
uint32_t UsbCdc_RxBytes(void)     { return s_rxBytes; }
uint32_t UsbCdc_TxBytes(void)     { return s_txBytes; }
uint32_t UsbCdc_TxDropBytes(void) { return s_txDropBytes; }

/**
  * @brief  由 usbd_conf.c 的 HAL_PCD_ResetCallback 间接触发（见下）
  * @note   之所以单独暴露：USB 总线复位次数是判断"主机是否在反复重试枚举"
  *         的最直接指标（枚举失败时 Windows 会不断复位重试）。
  */
void UsbCdc_NotifyReset(void)
{
  s_resetCnt++;
  s_ready = 0U;
}
