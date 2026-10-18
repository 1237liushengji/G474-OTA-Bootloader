/**
  ******************************************************************************
  * @file    usbd_conf.c
  * @brief   USB Device Library 与 STM32G474 HAL_PCD 之间的适配层（USBD_LL_*）
  *          + USB 时钟/引脚初始化（MSP）+ USB 中断入口
  *
  * 关键硬件配置说明（每一项都有原因）：
  *   1) 时钟：HSI48 选作 48MHz USB 时钟源，并用 CRS 以 USB SOF 为基准自动校准。
  *      为什么不用主 PLL 的 Q 输出：本工程 SYSCLK=150MHz 由 HSE+PLL 产生，
  *      要同时得到 150MHz 和 48MHz，需要 VCO 同时是 150 和 48 的整数倍，
  *      在 G4 允许的 VCO 范围内无解（LCM=1200MHz 远超上限）。
  *      故 HSI48 + CRS 是唯一干净方案（USB 全速要求 ±0.25%，CRS 可满足）。
  *
  *   2) 引脚：PA11 = USB_DM，PA12 = USB_DP，复用功能 AF10。
  *      D+ 上拉由芯片内部提供（USB_BCDR.DPPU），无需外部 1.5kΩ 电阻。
  *
  *   3) VBUS 检测：**必须关闭**。G474 的 USB_VBUS 复用引脚是 PA9，
  *      而 PA9 在本工程被 USART1_TX 占用（控制台日志输出）。且本板为自供电，
  *      不需要 VBUS 检测。故 Init 里不启用 VBUS sensing
  *      （本版 HAL 的 USB_CfgTypeDef 本身也没有该字段，默认即关闭）。
  ******************************************************************************
  */
#include "usbd_core.h"
#include "usbd_def.h"
#include "usbd_conf.h"
#include "usb_cdc.h"                              /* UsbCdc_NotifyReset() */

/*------------------------------------------------------------------ 句柄 ----*/
PCD_HandleTypeDef hpcd_USB_FS;

/*-------------------------------------------------------- 静态内存池 --------*/
/* 8 字节对齐，满足 USB 描述符 / 句柄的对齐要求 */
static uint64_t s_usbPool[256];                 /* 2KB */

void *USBD_static_malloc(uint32_t size)
{
  (void)size;                                   /* 只用一块固定池，size 可忽略 */
  return (void *)s_usbPool;
}

void USBD_static_free(void *p)
{
  (void)p;                                      /* 静态池无需释放 */
}

/*----------------------------------------------------- MSP：时钟/引脚/中断 --*/
void HAL_PCD_MspInit(PCD_HandleTypeDef *hpcd)
{
  GPIO_InitTypeDef         gpio = {0};
  RCC_PeriphCLKInitTypeDef periphClk = {0};
  RCC_CRSInitTypeDef       crs = {0};
  uint32_t                 t0;

  if (hpcd->Instance != USB)
  {
    return;
  }

  /* ---- 1) 打开 HSI48 ----
   * 【为什么不用 HAL_RCC_OscConfig】实测它在本工程里没能把 HSI48 拉起来
   *（RCC->CRRCR 全 0，HSI48ON/HSI48RDY 都是 0），原因未查明；
   * 而 HSI48 又是 USB 的唯一时钟源（见下），不起来则 USB 必不工作。
   * 这里改为**直接写寄存器**，行为确定、无隐藏前置条件：
   *   RCC->CRRCR.HSI48ON = 1 → 启动；随后等 RCC->CRRCR.HSI48RDY 置位。
   * 若 100ms 内仍未 ready，说明硬件/时钟树不可用，此时直接放弃 USB，
   * 但**绝不**卡死（否则整机起不来）。 */
  RCC->CRRCR |= RCC_CRRCR_HSI48ON;
  t0 = HAL_GetTick();
  while ((RCC->CRRCR & RCC_CRRCR_HSI48RDY) == 0U)
  {
    if ((HAL_GetTick() - t0) > 100U)
    {
      break;                       /* HSI48 起不来 → 放弃 USB，但继续跑主程序 */
    }
  }
  if ((RCC->CRRCR & RCC_CRRCR_HSI48RDY) == 0U)
  {
    return;                         /* 没有 48MHz 时钟就别配 USB 外设 */
  }

  /* ---- 2) USB 外设时钟源选 HSI48 ---- */
  periphClk.PeriphClockSelection = RCC_PERIPHCLK_USB;
  periphClk.UsbClockSelection    = RCC_USBCLKSOURCE_HSI48;
  (void)HAL_RCCEx_PeriphCLKConfig(&periphClk);

  /* ---- 3) CRS：用 USB SOF 自动微调 HSI48（USB 全速要求 ±0.25%）----
   * 说明：CRS 的同步源是 USB 的 SOF 包，只有在主机开始发送 SOF 后才生效，
   * 因此这里配置好并打开，后续由硬件自动收敛。 */
  __HAL_RCC_CRS_CLK_ENABLE();
  crs.Prescaler             = RCC_CRS_SYNC_DIV1;
  crs.Source                = RCC_CRS_SYNC_SOURCE_USB;
  crs.Polarity              = RCC_CRS_SYNC_POLARITY_RISING;
  crs.ReloadValue           = RCC_CRS_RELOADVALUE_DEFAULT;
  crs.ErrorLimitValue       = RCC_CRS_ERRORLIMIT_DEFAULT;
  crs.HSI48CalibrationValue = RCC_CRS_HSI48CALIBRATION_DEFAULT;
  HAL_RCCEx_CRSConfig(&crs);
  CRS->CR |= CRS_CR_CEN | CRS_CR_AUTOTRIMEN;    /* 使能误差计数 + 自动校准 */

  /* ---- 4) USB 外设时钟 + GPIO ---- */
  __HAL_RCC_USB_CLK_ENABLE();
  __HAL_RCC_GPIOA_CLK_ENABLE();

  /* PA11 = USB_DM, PA12 = USB_DP, AF10 */
  gpio.Pin       = GPIO_PIN_11 | GPIO_PIN_12;
  gpio.Mode      = GPIO_MODE_AF_PP;
  gpio.Pull      = GPIO_NOPULL;
  gpio.Speed     = GPIO_SPEED_FREQ_HIGH;
  gpio.Alternate = 10U;                         /* AF10 = USB_DM/DP（本 HAL 无对应宏，直接用数值） */
  HAL_GPIO_Init(GPIOA, &gpio);

  /* ⚠️【关键】这里**绝不**能使能 NVIC 的 USB_LP_IRQn！
   *
   * 踩过的坑（本会话实测到的 MemManage 故障根因）：
   *   HAL_PCD_MspInit 是被 HAL_PCD_Init 在**内部**调用的，而此刻
   *   `hpcd_USB_FS.pData`（指向 USBD_HandleTypeDef 的反向指针）**还没赋值**。
   *   一旦在这里 EnableIRQ，USB 外设已使能，中断会立刻触发：
   *     USB_LP_IRQHandler → HAL_PCD_IRQHandler → HAL_PCD_ResetCallback
   *       → USBD_LL_Reset((USBD_HandleTypeDef *)hpcd->pData)   // pData == NULL
   *         → USBD_Reset(NULL) → 写 NULL->dev_state → 故障。
   *   表现为：APP 刚启动就打印 "!! MEMMANAGE FAULT !!"（且 CFSR/HFSR 都是 0，
   *   因为故障是由"空指针写"引起的，读异常状态寄存器时已无从体现）。
   *   现象特征：故障点随中断时机漂移（有时在打印中途、有时更早）。
   *
   * 正确做法：NVIC 使能放到 USBD_LL_Init 的**最后**，等
   *   pdev->pData / hpcd.pData 都已接好、EP0 也开好了再开中断。
   */
}

void HAL_PCD_MspDeInit(PCD_HandleTypeDef *hpcd)
{
  if (hpcd->Instance != USB)
  {
    return;
  }

  __HAL_RCC_USB_CLK_DISABLE();
  HAL_NVIC_DisableIRQ(USB_LP_IRQn);

  /* 释放 PA11/PA12 回模拟态，避免残留 */
  HAL_GPIO_DeInit(GPIOA, GPIO_PIN_11 | GPIO_PIN_12);
}

/*-------------------------------------------------------------- USBD_LL ----*/
USBD_StatusTypeDef USBD_LL_Init(USBD_HandleTypeDef *pdev)
{
  /* ⚠️【顺序至关重要】必须**先**把双向指针接好，再让 HAL 去初始化外设。
   * 原因：HAL_PCD_Init 内部会调用 HAL_PCD_MspInit，而 USB 中断一旦被使能
   * 就会立刻触发回调；回调里要用 hpcd->pData 反查到 USBD 句柄。
   * 若此时 pData 仍为 NULL，就会以 NULL 进入 USBD_LL_* → NULL 解引用故障。 */
  pdev->pData              = &hpcd_USB_FS;
  hpcd_USB_FS.pData        = pdev;

  hpcd_USB_FS.Instance                 = USB;
  hpcd_USB_FS.Init.dev_endpoints       = 8U;
  hpcd_USB_FS.Init.speed               = PCD_SPEED_FULL;
  hpcd_USB_FS.Init.ep0_mps             = 64U;
  hpcd_USB_FS.Init.phy_itface          = PCD_PHY_EMBEDDED;
  hpcd_USB_FS.Init.Sof_enable          = DISABLE;
  hpcd_USB_FS.Init.low_power_enable    = DISABLE;
  hpcd_USB_FS.Init.lpm_enable          = DISABLE;
  hpcd_USB_FS.Init.battery_charging_enable = DISABLE;

  if (HAL_PCD_Init(&hpcd_USB_FS) != HAL_OK)
  {
    return USBD_FAIL;
  }

  /* EP0 双向打开（经典内核里 EP0 的 IN/OUT 是分开的两个逻辑端点） */
  if ((HAL_PCD_EP_Open(&hpcd_USB_FS, 0x00U, 64U, EP_TYPE_CTRL) != HAL_OK) ||
      (HAL_PCD_EP_Open(&hpcd_USB_FS, 0x80U, 64U, EP_TYPE_CTRL) != HAL_OK))
  {
    return USBD_FAIL;
  }

  /* 最后一步才使能 USB 中断（理由见上；MspInit 里刻意不使能）
   * 【A/B 判定实验】暂时注释掉，看 MemManage 故障是否随之中断消失。 */
  HAL_NVIC_SetPriority(USB_LP_IRQn, 2, 0);
  HAL_NVIC_EnableIRQ(USB_LP_IRQn);   /* HSI48 已修好，恢复中断 */

  return USBD_OK;
}

USBD_StatusTypeDef USBD_LL_DeInit(USBD_HandleTypeDef *pdev)
{
  (void)HAL_PCD_DeInit(pdev->pData);
  return USBD_OK;
}

USBD_StatusTypeDef USBD_LL_Start(USBD_HandleTypeDef *pdev)
{
  return (HAL_PCD_Start(pdev->pData) == HAL_OK) ? USBD_OK : USBD_FAIL;
}

USBD_StatusTypeDef USBD_LL_Stop(USBD_HandleTypeDef *pdev)
{
  return (HAL_PCD_Stop(pdev->pData) == HAL_OK) ? USBD_OK : USBD_FAIL;
}

USBD_StatusTypeDef USBD_LL_OpenEP(USBD_HandleTypeDef *pdev, uint8_t ep_addr,
                                  uint8_t ep_type, uint16_t ep_mps)
{
  return (HAL_PCD_EP_Open(pdev->pData, ep_addr, ep_mps, ep_type) == HAL_OK)
         ? USBD_OK : USBD_FAIL;
}

USBD_StatusTypeDef USBD_LL_CloseEP(USBD_HandleTypeDef *pdev, uint8_t ep_addr)
{
  return (HAL_PCD_EP_Close(pdev->pData, ep_addr) == HAL_OK) ? USBD_OK : USBD_FAIL;
}

USBD_StatusTypeDef USBD_LL_FlushEP(USBD_HandleTypeDef *pdev, uint8_t ep_addr)
{
  return (HAL_PCD_EP_Flush(pdev->pData, ep_addr) == HAL_OK) ? USBD_OK : USBD_FAIL;
}

USBD_StatusTypeDef USBD_LL_StallEP(USBD_HandleTypeDef *pdev, uint8_t ep_addr)
{
  return (HAL_PCD_EP_SetStall(pdev->pData, ep_addr) == HAL_OK) ? USBD_OK : USBD_FAIL;
}

USBD_StatusTypeDef USBD_LL_ClearStallEP(USBD_HandleTypeDef *pdev, uint8_t ep_addr)
{
  return (HAL_PCD_EP_ClrStall(pdev->pData, ep_addr) == HAL_OK) ? USBD_OK : USBD_FAIL;
}

uint8_t USBD_LL_IsStallEP(USBD_HandleTypeDef *pdev, uint8_t ep_addr)
{
  PCD_HandleTypeDef *hpcd = (PCD_HandleTypeDef *)pdev->pData;

  if ((ep_addr & 0x80U) == 0x80U)
  {
    return hpcd->IN_ep[ep_addr & 0x7FU].is_stall;
  }
  return hpcd->OUT_ep[ep_addr & 0x7FU].is_stall;
}

USBD_StatusTypeDef USBD_LL_SetUSBAddress(USBD_HandleTypeDef *pdev, uint8_t dev_addr)
{
  return (HAL_PCD_SetAddress(pdev->pData, dev_addr) == HAL_OK) ? USBD_OK : USBD_FAIL;
}

USBD_StatusTypeDef USBD_LL_Transmit(USBD_HandleTypeDef *pdev, uint8_t ep_addr,
                                    uint8_t *pbuf, uint16_t size)
{
  return (HAL_PCD_EP_Transmit(pdev->pData, ep_addr, pbuf, size) == HAL_OK)
         ? USBD_OK : USBD_FAIL;
}

USBD_StatusTypeDef USBD_LL_PrepareReceive(USBD_HandleTypeDef *pdev, uint8_t ep_addr,
                                          uint8_t *pbuf, uint16_t size)
{
  return (HAL_PCD_EP_Receive(pdev->pData, ep_addr, pbuf, size) == HAL_OK)
         ? USBD_OK : USBD_FAIL;
}

uint32_t USBD_LL_GetRxDataSize(USBD_HandleTypeDef *pdev, uint8_t ep_addr)
{
  return HAL_PCD_EP_GetRxCount((PCD_HandleTypeDef const *)pdev->pData, ep_addr);
}

void USBD_LL_Delay(uint32_t Delay)
{
  HAL_Delay(Delay);
}

/*------------------------------------------- HAL_PCD 回调 → 库回调 --------*/
void HAL_PCD_SetupStageCallback(PCD_HandleTypeDef *hpcd)
{
  USBD_LL_SetupStage((USBD_HandleTypeDef *)hpcd->pData, (uint8_t *)hpcd->Setup);
}

void HAL_PCD_DataOutStageCallback(PCD_HandleTypeDef *hpcd, uint8_t epnum)
{
  USBD_LL_DataOutStage((USBD_HandleTypeDef *)hpcd->pData, epnum,
                       hpcd->OUT_ep[epnum].xfer_buff);
}

void HAL_PCD_DataInStageCallback(PCD_HandleTypeDef *hpcd, uint8_t epnum)
{
  USBD_LL_DataInStage((USBD_HandleTypeDef *)hpcd->pData, epnum,
                      hpcd->IN_ep[epnum].xfer_buff);
}

void HAL_PCD_SOFCallback(PCD_HandleTypeDef *hpcd)
{
  USBD_LL_SOF((USBD_HandleTypeDef *)hpcd->pData);
}

void HAL_PCD_ResetCallback(PCD_HandleTypeDef *hpcd)
{
  USBD_SpeedTypeDef speed = USBD_SPEED_FULL;

  UsbCdc_NotifyReset();                          /* 计数：判断主机是否在反复重试枚举 */
  USBD_LL_SetSpeed((USBD_HandleTypeDef *)hpcd->pData, speed);
  USBD_LL_Reset((USBD_HandleTypeDef *)hpcd->pData);
}

void HAL_PCD_SuspendCallback(PCD_HandleTypeDef *hpcd)
{
  USBD_LL_Suspend((USBD_HandleTypeDef *)hpcd->pData);
}

void HAL_PCD_ResumeCallback(PCD_HandleTypeDef *hpcd)
{
  USBD_LL_Resume((USBD_HandleTypeDef *)hpcd->pData);
}

void HAL_PCD_ConnectCallback(PCD_HandleTypeDef *hpcd)
{
  USBD_LL_DevConnected((USBD_HandleTypeDef *)hpcd->pData);
}

void HAL_PCD_DisconnectCallback(PCD_HandleTypeDef *hpcd)
{
  USBD_LL_DevDisconnected((USBD_HandleTypeDef *)hpcd->pData);
}

void HAL_PCD_ISOOUTIncompleteCallback(PCD_HandleTypeDef *hpcd, uint8_t epnum)
{
  USBD_LL_IsoOUTIncomplete((USBD_HandleTypeDef *)hpcd->pData, epnum);
}

void HAL_PCD_ISOINIncompleteCallback(PCD_HandleTypeDef *hpcd, uint8_t epnum)
{
  USBD_LL_IsoINIncomplete((USBD_HandleTypeDef *)hpcd->pData, epnum);
}

void HAL_PCD_ErrorCallback(PCD_HandleTypeDef *hpcd)
{
  (void)hpcd;
  /* 非致命：USB 出错由库/主机侧重试处理，不进入死循环，避免 IWDG 复位 */
}

/*----------------------------------------------------------- USB 中断 ------*/
void USB_LP_IRQHandler(void)
{
  HAL_PCD_IRQHandler(&hpcd_USB_FS);
}

void USB_HP_IRQHandler(void)
{
  /* 本工程未使用双缓冲/等时端点，HP 中断理论上不会触发；
   * 仍提供入口以防 NVIC 中意外使能导致跳飞（跳到默认弱符号会死循环）。 */
  HAL_PCD_IRQHandler(&hpcd_USB_FS);
}
