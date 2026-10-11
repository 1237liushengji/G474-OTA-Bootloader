/* USER CODE BEGIN Header */
/**
  ******************************************************************************
  * @file           : main.c
  * @brief          : 双机CAN通信应用（两块DRG ST-10 STM32G474VET6板 + SN65HVD230）
  *
  * 应用层协议（详见 can.h 与工程README）：
  *   板A（主节点，CAN_NODE_ROLE = CAN_NODE_A）：
  *     - 每1000ms发送一帧命令帧（ID 0x321，载荷含序号seq与开机秒数）；
  *     - 收到板B应答帧（ID 0x322）后刷新链路存活时间，并按应答中的
  *       LED状态同步点亮/熄灭本板LED1（两板LED闪烁同步）；
  *     - 超过CAN_LINK_TIMEOUT_MS未收到应答 -> LED1以100ms快闪指示链路故障。
  *   板B（从节点，CAN_NODE_ROLE = CAN_NODE_B）：
  *     - 收到命令帧后翻转本板LED1，并回发应答帧（回显seq、LED状态、累计帧数）；
  *     - 超过CAN_LINK_TIMEOUT_MS未收到命令 -> LED1快闪指示链路故障。
  *
  * 时钟：HSE 8MHz -> PLL(M=2,N=75,R=2) -> SYSCLK/HCLK/PCLK1 = 150MHz
  * FDCAN内核时钟 = PCLK1 = 150MHz，500kbit/s经典CAN（位时序见can.h）
  ******************************************************************************
  * @attention
  *
  * Copyright (c) 2024 STMicroelectronics.
  * All rights reserved.
  *
  * This software is licensed under terms that can be found in the LICENSE file
  * in the root directory of this software component.
  * If no LICENSE file comes with this software, it is provided AS-IS.
  *
  ******************************************************************************
  */
/* USER CODE END Header */
/* Includes ------------------------------------------------------------------*/
#include "main.h"
#include "gpio.h"

/* Private includes ----------------------------------------------------------*/
/* USER CODE BEGIN Includes */
#include "led.h"
#include "can.h"
#include "bsp_uart.h"
#include "console.h"
#include "isotp.h"
#include "uds.h"
#include "ota_agent.h"   /* OTA：刷写服务与状态管理 */
#include "ota_tunnel.h"  /* OTA：串口<->诊断CAN隧道 */
#include "bsp_lcd.h"      /* LCD：ST7789 240x280 状态屏 */
#include <stdio.h>        /* snprintf：LCD 状态行格式化 */
/* USER CODE END Includes */

/* Private typedef -----------------------------------------------------------*/
/* USER CODE BEGIN PTD */

/* USER CODE END PTD */

/* Private define ------------------------------------------------------------*/
/* USER CODE BEGIN PD */
#define APP_CMD_PERIOD_MS        1000U    /* 板A命令帧发送周期 */
#define APP_ERR_BLINK_PERIOD_MS  100U     /* 链路故障指示快闪周期 */
/* USER CODE END PD */

/* Private macro -------------------------------------------------------------*/
/* USER CODE BEGIN PM */

/* USER CODE END PM */

/* Private variables ---------------------------------------------------------*/

/* USER CODE BEGIN PV */
/* HSE 起振失败标志：1 = 已回落 HSI+PLL（启动日志会上报） */
uint8_t g_hseFailed = 0U;
static CAN_RxMsg s_rxMsg;                          /* 最近一帧接收报文 */

static uint32_t  s_bootTick;                       /* 开机时刻（ms） */
static uint32_t  s_lastCmdTick;                    /* 板A：最近发送/板B：最近收到命令帧时刻 */
static uint32_t  s_lastRespTick;                   /* 板A：最近收到应答帧时刻 */
static uint32_t  s_lastBlinkTick;                  /* 故障指示快闪节拍 */
static uint8_t   s_seq;                            /* 板A命令帧序号 */
static uint8_t   s_bLedState;                      /* 板B当前LED状态（两板同步显示） */
static uint16_t  s_rxCmdCount;                     /* 板B累计收到命令帧数 */
static uint8_t   s_linkEverOk;                     /* 链路是否曾经建立 */
static uint32_t  s_lastErrBlinkTick;               /* LED2错误指示闪烁节拍 */
static uint32_t  s_errTick;                        /* 最近一次总线错误事件时刻 */
static uint32_t  s_errTotalLast;                   /* 上次统计的错误总数快照 */
static uint8_t   s_errActive;                      /* 错误指示窗口激活标志 */

IWDG_HandleTypeDef hiwdg;                          /* 独立看门狗：LSI/32=1kHz，重装载3s
                                                    * 非 static：ota_agent.c 在 Flash 擦写前喂狗需访问 */

/* 调试计数器（定位sniff下LED冻结：观察协议分发路径是否执行） */
uint32_t g_dbgHx;                                  /* App_HandleRx调用次数 */
uint32_t g_dbgCmd;                                 /* App_HandleCmd调用次数 */
uint32_t g_dbgResp;                                /* App_HandleResp调用次数 */
uint32_t g_dbgPoll;                                /* CAN_PollRx取到帧次数 */
/* USER CODE END PV */

/* Private function prototypes -----------------------------------------------*/
void SystemClock_Config(void);
/* USER CODE BEGIN PFP */
static void App_HandleCmd(void);
static void App_HandleResp(void);
static void App_HandleRx(void);
static void App_Process(void);
/* USER CODE END PFP */

/* Private user code ---------------------------------------------------------*/
/* USER CODE BEGIN 0 */

/**
  * @brief  主节点侧：处理应答帧（ID 0x322）——刷新链路状态并同步LED
  */
static void App_HandleResp(void)
{
  g_dbgResp++;
  s_lastRespTick = HAL_GetTick();
  s_linkEverOk   = 1U;
  s_bLedState    = (s_rxMsg.data[1] != 0U) ? 1U : 0U;

  GPIOE->BSRR = (s_bLedState != 0U) ? GPIO_BSRR_BR0 : GPIO_BSRR_BS0;  /* 低电平点亮 */
}

/**
  * @brief  从节点侧：处理命令帧（ID 0x321）——翻转LED并立即应答
  * @note   LED采用"软件状态为唯一真相+BSRR原子直写"：低电平点亮，state=1时
  *         写BR0(复位)，state=0时写BS0(置位)。不再使用读-改-写式翻转与
  *         引脚读回（排查sniff下TogglePin写入不保持的问题）。
  */
static void App_HandleCmd(void)
{
  g_dbgCmd++;
  s_lastCmdTick = HAL_GetTick();
  s_linkEverOk  = 1U;
  s_rxCmdCount++;

  s_bLedState ^= 1U;                                    /* 状态翻转（软件真相） */
  GPIOE->BSRR = (s_bLedState != 0U) ? GPIO_BSRR_BR0 : GPIO_BSRR_BS0;

  CAN_EncodeResp(s_rxMsg.data, s_rxMsg.data[0], s_bLedState, s_rxCmdCount);
  (void)CAN_Send(CAN_ID_RESP_B2A, s_rxMsg.data, CAN_PAYLOAD_LEN);
}

/**
  * @brief  处理一帧接收报文（按节点角色分发；自测试模式单板扮演双角色）
  */
static void App_HandleRx(void)
{
  g_dbgHx++;

  /* 诊断路由：ISO-TP模块按自身rxId过滤，非诊断帧直接忽略 */
  IsoTp_OnCanFrame(s_rxMsg.id, s_rxMsg.data, s_rxMsg.len);

#if (CAN_DEBUG_SELFTEST != 0)
  /* 单板自测试：回环收到自己发的命令帧，同时扮演两个节点 */
  if (s_rxMsg.id == CAN_ID_CMD_A2B)
  {
    App_HandleCmd();
  }
  else if (s_rxMsg.id == CAN_ID_RESP_B2A)
  {
    App_HandleResp();
  }
  else
  {
    /* 其他ID不处理 */
  }
#elif (CAN_NODE_ROLE == CAN_NODE_A)
  /* 板A：处理板B的应答帧（ID 0x322） */
  if (s_rxMsg.id == CAN_ID_RESP_B2A)
  {
    App_HandleResp();
  }
#else
  /* 板B：处理板A的命令帧（ID 0x321），翻转LED并应答 */
  if (s_rxMsg.id == CAN_ID_CMD_A2B)
  {
    App_HandleCmd();
  }
#endif
}

/**
  * @brief  主循环周期任务：收帧处理 + 发送调度 + 链路监视
  */
/**
  * @brief  LCD 测试画面：每秒刷新一次，用来证明"屏在活、且在持续刷新"。
  * @note   刻意不用整屏重绘：软 SPI 全屏约 1M bit，耗时近 1 秒会拖慢主循环。
  *         这里只改动的行（LCD_PrintField 定宽覆盖，短值不留残影）。
  */
static void LcdUi_Task(void)
{
  static uint32_t s_lastLcd = 0U;
  static uint32_t s_boots  = 0U;
  char            line[24];
  uint32_t        now = HAL_GetTick();

  if ((now - s_lastLcd) < 1000U)
  {
    return;
  }
  s_lastLcd = now;
  s_boots++;

  (void)snprintf(line, sizeof(line), "uptime %lus", (unsigned long)(now / 1000U));
  LCD_PrintField(5, 0, line, LCD_CHAR_COLS, LCD_COLOR_WHITE, LCD_COLOR_BLACK);

  (void)snprintf(line, sizeof(line), "tick %lu", (unsigned long)s_boots);
  LCD_PrintField(6, 0, line, LCD_CHAR_COLS, LCD_COLOR_YELLOW, LCD_COLOR_BLACK);

  /* 第 8 行交替 "|" 与空格：即使字库/坐标有问题，也能看出刷新确实在发生 */
  LCD_Print(8, 0, ((s_boots & 1U) != 0U) ? "|" : " ", LCD_COLOR_RED, LCD_COLOR_BLACK);
}

static void App_Process(void)
{
  uint32_t now = HAL_GetTick();

  /* 1. 取走并处理接收报文：监视模式下"打印+协议处理"并行——观察不打扰业务 */
  if (CAN_PollRx(&s_rxMsg))
  {
    g_dbgPoll++;
    if (Console_SniffEnabled())
    {
      Console_PrintFrame(&s_rxMsg);
    }
    OtaTunnel_PrintFrame(&s_rxMsg);                /* 隧道开启时回吐 0x7E0/0x7E8（原始帧） */
    App_HandleRx();
  }

  /* 2. 总线错误指示（LED2绿色）：出现bus-off/错误被动事件后5秒内以250ms闪烁。
   *    发送端发帧无人应答（ACK缺失）时必然出现错误被动→bus-off，据此可判断
   *    "本板在发、但对端收发器不在总线上"；无任何错误则说明总线电气层沉默。 */
  {
    const CAN_Stats *st = CAN_GetStats();
    uint32_t errTotal = st->busOffCount + st->errPassiveCount;

    if (errTotal != s_errTotalLast)
    {
      s_errTotalLast = errTotal;
      s_errTick      = now;
      s_errActive    = 1U;
    }

    if (s_errActive != 0U)
    {
      if ((now - s_errTick) < 5000U)
      {
        if ((now - s_lastErrBlinkTick) >= 250U)
        {
          s_lastErrBlinkTick = now;
          LED2_Toggle;
        }
      }
      else
      {
        s_errActive = 0U;
        LED2_OFF;
      }
    }
  }

#if (CAN_NODE_ROLE == CAN_NODE_A)
  /* 2. 板A：周期发送命令帧
   *
   * ⚠️ 【2026-10-10 临时禁用】CAN 发送在真机上会挂死 AHB 总线（详见 can.c 内
   * 与 docs/APP启动重启循环-诊断报告-2026-10-10.md 的记录）：
   * 只要写 TXBAR 的请求位（真正请求发一帧），总线就被挂住约 3 秒直到 IWDG 复位。
   * 已排除的因素：内核时钟源/位时序（HSE 8MHz 与 PCLK1 150MHz 均挂）、
   * 自动重传开关、回环/正常模式、消息 RAM 地址与可达性、以及控制器状态
   * （发送前实测 CCCR=0/ECR=0/TXBC=0/TXFQS=3/TXBRP=0，一切正常）。
   * 在找到根因前，此处禁用周期发送，使主循环能够运行（这也是串口命令可用的前提）。
   * 修复 CAN 发送后把 CAN_HEARTBEAT_ENABLE 改回 1 即可恢复。 */
#ifndef CAN_HEARTBEAT_ENABLE
#define CAN_HEARTBEAT_ENABLE 0
#endif

#if (CAN_HEARTBEAT_ENABLE != 0)
  if ((now - s_lastCmdTick) >= APP_CMD_PERIOD_MS)
  {
    s_lastCmdTick = now;
    s_seq++;

    uint8_t payload[CAN_PAYLOAD_LEN];
    CAN_EncodeCmd(payload, s_seq, (uint16_t)((now - s_bootTick) / 1000U));
    (void)CAN_Send(CAN_ID_CMD_A2B, payload, CAN_PAYLOAD_LEN);
  }
#else
  s_lastCmdTick = now;                             /* 保持计时器推进，便于恢复时行为一致 */
#endif

  /* 3. 板A链路监视：曾建立链路后超过门限无应答 -> 快闪报警 */
  if (s_linkEverOk != 0U)
  {
    if ((now - s_lastRespTick) > CAN_LINK_TIMEOUT_MS)
    {
      if ((now - s_lastBlinkTick) >= APP_ERR_BLINK_PERIOD_MS)
      {
        s_lastBlinkTick = now;
        LED1_Toggle;
      }
    }
  }
  else
  {
    /* 上电后从未建立链路：给首个周期收发建立时间后同样快闪提示 */
    if (((now - s_bootTick) > (CAN_LINK_TIMEOUT_MS + APP_CMD_PERIOD_MS)) &&
        ((now - s_lastBlinkTick) >= APP_ERR_BLINK_PERIOD_MS))
    {
      s_lastBlinkTick = now;
      LED1_Toggle;
    }
  }
#else
  /* 板B链路监视：曾收到命令后超过门限无新命令 -> 快闪报警 */
  if (s_linkEverOk != 0U)
  {
    if ((now - s_lastCmdTick) > CAN_LINK_TIMEOUT_MS)
    {
      if ((now - s_lastBlinkTick) >= APP_ERR_BLINK_PERIOD_MS)
      {
        s_lastBlinkTick = now;
        LED1_Toggle;
      }
    }
  }
#endif
}

/* USER CODE END 0 */

/**
  * @brief  应用层LED钩子（uds.c的0x2E服务调用）：状态驱动+BSRR原子直写
  */
void App_SetLed(uint8_t on)
{
  s_bLedState = (on != 0U) ? 1U : 0U;
  GPIOE->BSRR = (on != 0U) ? GPIO_BSRR_BR0 : GPIO_BSRR_BS0;   /* 低电平点亮 */
}

uint8_t App_GetLed(void)
{
  return s_bLedState;
}

/**
  * @brief  启动早期的"边延时边喂狗"延时（每 ≤20 万次空转喂一次）
  *
  * 为什么必须有：BL 跳转前，独立看门狗（IWDG，3s）**已经在跑**，而 IWDG 一旦启动
  * 就**无法关闭** —— 只能由 APP 接着喂，直到主循环接管。可 APP 启动早期偏偏有几段
  * 长延时（3 段 LED 打点，HSI 16MHz 下合计约 2~3s；LCD_Init 另有 ~760ms），
  * 累计超过 3s 就会被狗拉回。
  *
  * 实测现象（PC 采样）：APP 已经跑到 `UART_SetConfig`，却在打印横幅**之前**被复位，
  * BL 与 APP 的代码在采样里交替出现 —— 就是这条原因。
  *
  * 因此把延时切块，每块 ≤20 万次空转（16MHz 下约 0.1s）就喂一次狗。
  * 喂狗只要求 Instance 有效；IWDG 配置沿用 BL 留下的（LSI/32, reload 3000）。
  *
  * ⚠️ `noinline` 不能省：函数体一旦被内联，"切块上界"就成了编译期常量，
  *    ARMCLANG 会把空转循环**整体展开** —— 实测 main.o 的 .text.main 从约 1KB
  *    涨到 12552 字节（正是 BL 侧 `BL_DiagDelay` 记录过的同一个坑）。
  *    加 noinline 后上界变成运行期值，编译器不再展开。
  */
__attribute__((noinline))
static void App_EarlyDelay(uint32_t nops)
{
  hiwdg.Instance = IWDG;                     /* HAL 经 Instance 访问寄存器，必须赋值 */

  while (nops != 0U)
  {
    uint32_t chunk = (nops > 200000U) ? 200000U : nops;

    nops -= chunk;
    while (chunk-- != 0U)
    {
      __NOP();
    }
    (void)HAL_IWDG_Refresh(&hiwdg);          /* 即使还在 HSI 16MHz，间隔也远小于 3s */
  }
}

/**
  * @brief  The application entry point.
  * @retval int
  */
int main(void)
{
  /* ================= APP 早期 LED 打点 =================
   * 为什么需要：HAL_Init() / SystemClock_Config() 都在 BSP_UART_Init() 之前，
   * 若卡在那一带，串口永远不会有输出，外部完全无法观察（"上电没反应"）。
   * 这里用**直接寄存器**操作 LED(PE0, 低有效)，不依赖 HAL、不依赖串口、
   * 不依赖系统时钟配置，因此最早就能给出可观察信号。
   * 用法：闪 N 下 = 走到第 N 步；节奏 150ms 亮 / 250ms 灭，组间停 800ms。
   *
   * ⚠️ 每一段延时都必须**边延时边喂狗**（见 App_EarlyDelay 的说明）——
   *    否则 APP 会在启动早期被 BL 留下的独立看门狗拉回，横幅永远打不出来。
   * ==================================================== */
  {
    RCC->AHB2ENR |= (1UL << 4);                       /* GPIOEEN */
    (void)RCC->AHB2ENR;
    GPIOE->MODER = (GPIOE->MODER & ~(3UL << 0)) | (1UL << 0);
    GPIOE->OTYPER &= ~(1UL << 0);
    GPIOE->OSPEEDR = (GPIOE->OSPEEDR & ~(3UL << 0)) | (3UL << 0);
  }

  /* USER CODE BEGIN 1 */

  /* USER CODE END 1 */

  /* MCU Configuration--------------------------------------------------------*/

  /* Reset of all peripherals, Initializes the Flash interface and the Systick. */
  HAL_Init();

  /* [APP-LED 1] HAL_Init 完成（能闪 1 下 = MCU 在执行 APP 代码） */
  for (uint32_t n = 0U; n < 1U; n++)
  {
    GPIOE->BRR = (1UL << 0);  App_EarlyDelay(600000U);
    GPIOE->BSRR = (1UL << 0); App_EarlyDelay(1000000U);
  }
  App_EarlyDelay(3200000U);

  /* USER CODE BEGIN Init */

  /* USER CODE END Init */

  /* Configure the system clock */
  SystemClock_Config();

  /* [APP-LED 2] 时钟配置完成（能闪 2 下 = HSE/回落都正常） */
  for (uint32_t n = 0U; n < 2U; n++)
  {
    GPIOE->BRR = (1UL << 0);  App_EarlyDelay(600000U);
    GPIOE->BSRR = (1UL << 0); App_EarlyDelay(1000000U);
  }
  App_EarlyDelay(3200000U);

  /* USER CODE BEGIN SysInit */

  /* USER CODE END SysInit */

  /* Initialize all configured peripherals */
  MX_GPIO_Init();
  /* USER CODE BEGIN 2 */
  LED_Init();                                      /* LED1（PE0，蓝色）作通信状态指示 */
  LED1_OFF;                                        /* 应用接管LED：初始熄灭 */

  BSP_UART_Init();                                 /* 控制台串口 USART1 PA9/PA10 115200 8N1 */

  /* [APP-LED 3] 串口就绪 —— 之后由 BSP_UART_Send 输出文字日志 */
  for (uint32_t n = 0U; n < 3U; n++)
  {
    GPIOE->BRR = (1UL << 0);  App_EarlyDelay(600000U);
    GPIOE->BSRR = (1UL << 0); App_EarlyDelay(1000000U);
  }
  Console_Init();                                  /* 打印横幅，等待命令 */
  BSP_UART_Send("main: led-bsrr-state M3\r\n", 24U); /* main.c构建指纹：无此行=烧的旧main.o */

  /* ================= 【2026-10-10 诊断】启动检查点 =================
   * 真机现象：APP 打印到上一行后，被 IWDG 每 ~3 秒复位一次，主循环从未跑起来
   * （所以串口 RX 看起来"完全无响应"——Console_Task 根本没机会执行）。
   * 本段逐步打点 + 打印 tick，把卡点缩到一个函数内；定位完成后可整段删除。
   * 判读：某条 [boot] 之后再无输出 => 卡在该检查点与其后继之间。
   * ================================================================ */
  BSP_UART_BootMark("A: tick alive check");
  {
    uint32_t          t0 = HAL_GetTick();
    volatile uint32_t spin;

    for (spin = 0U; spin < 600000U; spin++)        /* 忙等约 40ms@150MHz */
    {
      __NOP();
    }
    BSP_UART_Printf("[boot] tick0=%lu tick1=%lu delta=%lu (>0 => SysTick 在走)\r\n",
                    (unsigned long)t0, (unsigned long)HAL_GetTick(),
                    (unsigned long)(HAL_GetTick() - t0));
  }

  {
    HAL_StatusTypeDef can_st;

    BSP_UART_BootMark("B: before CAN_Init");

    /* 【2026-10-11 无串口诊断】进入前记录 tick + 抓一份寄存器快照。
     * 目的：即使串口全挂（P2），也能用 SWD 读 g_canDiag 判定 CAN 状态。 */
    g_canDiag.tick_before = HAL_GetTick();
    CAN_DiagCapture(CAN_DIAG_STAGE_INIT_ENTER);

    can_st = CAN_Init();                             /* FDCAN2 + 过滤器 + 中断 + 启动 */
    BSP_UART_BootMark("C: after CAN_Init");

    /* 抓"CAN_Init 之后"的寄存器 + 记录返回码与耗时。
     * 判据：nbtp 若不是 0（应为 0x0D0C0F01 一类的合法值）= 位时序已写入；
     *       init_status=0 = HAL_OK；tick_after-tick_before 应立即（非 3 秒）。 */
    g_canDiag.tick_after  = HAL_GetTick();
    g_canDiag.init_status = (uint32_t)can_st;
    CAN_DiagCapture(CAN_DIAG_STAGE_INIT_DONE);

    if (can_st != HAL_OK)
    {
      /* ⚠️ 这里**绝不能** Error_Handler()：它关中断死循环，3 秒后被 IWDG 复位，
       * 现场只剩"APP 启动后约 3 秒重启"，一条线索都留不下（实测踩过：
       * 滴答冻结在 3482ms 不再增长，正是关中断的形态）。
       * 改为把失败码与 PCLK1 打出来并**继续跑** —— 控制台活着才能继续诊断
       * （CAN 不通时 OTA 隧道不可用，但串口命令行 stats/help 仍可用）。 */
      char dbg[112];
      int  n = snprintf(dbg, sizeof(dbg), "can: CAN_Init FAILED st=%d pclk1=%luHz\r\n",
                        (int)can_st, (unsigned long)HAL_RCC_GetPCLK1Freq());

      if (n > (int)(sizeof(dbg) - 1U))
      {
        n = (int)(sizeof(dbg) - 1U);
      }
      if (n > 0)
      {
        BSP_UART_Send(dbg, (uint16_t)n);
      }
    }
  }

  /* 上电自检：内部回环自发自收，验证FDCAN内核+消息RAM+中断+驱动软件链路 */
  {
    bool st;

    BSP_UART_BootMark("D: before CAN_SelfTest");
    st = CAN_SelfTest();
    BSP_UART_BootMark("E: after CAN_SelfTest");

    g_canDiag.selftest_ok = st ? 1U : 0U;
    CAN_DiagCapture(CAN_DIAG_STAGE_SELFTEST);

    if (st)
    {
      BSP_UART_Send("CAN self-test: PASS\r\n", 21U);
    }
    else
    {
      BSP_UART_Send("CAN self-test: FAIL\r\n", 21U);
    }
  }

  BSP_UART_BootMark("F: before Uds/OtaTunnel/Ota init");
  Uds_Init();                                      /* ISO-TP+UDS诊断栈（角色自动分配） */
  OtaTunnel_Init();                                /* OTA：串口<->诊断CAN隧道（默认关闭） */
  Ota_Init();                                      /* OTA：读控制区/识别本槽/置试运行待确认 */
  BSP_UART_BootMark("G: after Uds/OtaTunnel/Ota init");

  /* ---- LCD 状态屏（LCD_Init 内含约 760ms 延时，前后各喂一次狗，避免出现喂狗空窗）---- */
  BSP_UART_BootMark("H: before LCD/IWDG block");
  hiwdg.Instance = IWDG;
  (void)HAL_IWDG_Refresh(&hiwdg);
  LCD_Init();                                      /* 背光开 + 清屏为黑，失败则全白/花屏 */
  (void)HAL_IWDG_Refresh(&hiwdg);
  LCD_Print(0, 0, "STM32G474 OTA SYSTEM", LCD_COLOR_CYAN, LCD_COLOR_BLACK);
  LCD_Print(1, 0, "LCD OK / soft-SPI 240x280", LCD_COLOR_GREEN, LCD_COLOR_BLACK);
  LCD_Print(3, 0, "waiting for CAN link...", LCD_COLOR_GRAY, LCD_COLOR_BLACK);

  /* 独立看门狗：主循环喂狗；调试器halt时冻结计数，断点调试不误复位 */
  DBGMCU->APB1FZR1 |= DBGMCU_APB1FZR1_DBG_IWDG_STOP;
  hiwdg.Instance     = IWDG;                       /* HAL经Instance访问寄存器，必须赋值（漏赋=空指针HardFault） */
  hiwdg.Init.Prescaler = IWDG_PRESCALER_32;        /* LSI 32kHz/32 = 1kHz */
  hiwdg.Init.Reload    = 3000U;                    /* 3s超时（含裕量） */
  hiwdg.Init.Window    = IWDG_WINDOW_DISABLE;
  if (HAL_IWDG_Init(&hiwdg) != HAL_OK)
  {
    /* ⚠️ 这里**绝不能** Error_Handler() —— 这是"BL 交接"场景的必然分支：
     *   从 BL 跳转进来时，独立看门狗**已经在跑**，而 IWDG 一旦启动就无法重配置
     *   （PR/RLR 变为只读），HAL_IWDG_Init 等 PVU/RVU 标志会超时返回 HAL_TIMEOUT。
     *   本工程 APP 与 BL 的 IWDG 配置**完全相同**（LSI/32 = 1kHz、reload 3000 = 3s），
     *   所以直接沿用 BL 留下的配置即可，主循环照常 HAL_IWDG_Refresh。
     *
     *   曾经写成 Error_Handler() 的后果（实测）：关中断死循环 -> SysTick 停摆、
     *   APP 滴答冻结在 3482ms -> 3 秒后被 IWDG 复位 -> BL 重新跳转 -> 无限循环，
     *   APP 永远进不了主循环（现象是"LED 闪 1-2-3 后循环复位"）。 */
    static const char kIwdgMsg[] = "iwdg: already running (BL handover), keep feeding\r\n";
    BSP_UART_Send(kIwdgMsg, (uint16_t)(sizeof(kIwdgMsg) - 1U));
  }

  s_bootTick    = HAL_GetTick();
  s_lastCmdTick = s_bootTick;
  s_lastRespTick = s_bootTick;
  s_lastBlinkTick = s_bootTick;
  /* USER CODE END 2 */

  /* Infinite loop */
  /* USER CODE BEGIN WHILE */
  while (1)
  {
    App_Process();
    Console_Task();                                /* 串口命令行控制台 */
    BSP_UART_DiagTask();                           /* UART 接收链路自诊断（仅收不到时打印） */
    CAN_Task();                                    /* bus-off指数退避恢复调度 */
    IsoTp_Task();                                  /* ISO-TP：STmin节拍/超时 */
    Uds_Task();                                    /* UDS：S3会话/客户端超时 */
    Ota_Task();                                    /* OTA：试运行自检通过后落定 TRIAL->IDLE */
    LcdUi_Task();                                  /* LCD：1 秒刷新一次状态指征 */
    HAL_IWDG_Refresh(&hiwdg);                      /* 喂狗（主循环健康证明） */
    /* USER CODE END WHILE */

    /* USER CODE BEGIN 3 */
  }
  /* USER CODE END 3 */
}

/**
  * @brief System Clock Configuration
  * @retval None
  */
void SystemClock_Config(void)
{
  RCC_OscInitTypeDef RCC_OscInitStruct = {0};
  RCC_ClkInitTypeDef RCC_ClkInitStruct = {0};

  /** Configure the main internal regulator output voltage
  */
  HAL_PWREx_ControlVoltageScaling(PWR_REGULATOR_VOLTAGE_SCALE1);

  /** Initializes the RCC Oscillators according to the specified parameters
  * in the RCC_OscInitTypeDef structure.
  */
  RCC_OscInitStruct.OscillatorType = RCC_OSCILLATORTYPE_HSE;
  RCC_OscInitStruct.HSEState = RCC_HSE_ON;
  RCC_OscInitStruct.PLL.PLLState = RCC_PLL_ON;
  RCC_OscInitStruct.PLL.PLLSource = RCC_PLLSOURCE_HSE;
  RCC_OscInitStruct.PLL.PLLM = RCC_PLLM_DIV2;
  RCC_OscInitStruct.PLL.PLLN = 75;
  RCC_OscInitStruct.PLL.PLLP = RCC_PLLP_DIV2;
  RCC_OscInitStruct.PLL.PLLQ = RCC_PLLQ_DIV2;
  RCC_OscInitStruct.PLL.PLLR = RCC_PLLR_DIV2;
  if (HAL_RCC_OscConfig(&RCC_OscInitStruct) != HAL_OK)
  {
    /* HSE 起振失败 -> 回落 HSI + PLL（同为 150MHz，UART/CAN 时序一致）。
     * 原实现此处直接 Error_Handler() 死循环：现场表现为"上电完全没反应"，
     * 串口与 LED 都无输出，无法诊断。回落后至少还能起来并打印告警。 */
    RCC_OscInitStruct.OscillatorType      = RCC_OSCILLATORTYPE_HSI;
    RCC_OscInitStruct.HSIState            = RCC_HSI_ON;
    RCC_OscInitStruct.HSICalibrationValue = RCC_HSICALIBRATION_DEFAULT;
    RCC_OscInitStruct.PLL.PLLState        = RCC_PLL_ON;
    RCC_OscInitStruct.PLL.PLLSource       = RCC_PLLSOURCE_HSI;
    RCC_OscInitStruct.PLL.PLLM            = RCC_PLLM_DIV4;   /* HSI16/4 = 4MHz */
    RCC_OscInitStruct.PLL.PLLN            = 75;              /* x75 = 300MHz VCO */
    RCC_OscInitStruct.PLL.PLLP            = RCC_PLLP_DIV2;
    RCC_OscInitStruct.PLL.PLLQ            = RCC_PLLQ_DIV2;
    RCC_OscInitStruct.PLL.PLLR            = RCC_PLLR_DIV2;   /* /2 = 150MHz */
    if (HAL_RCC_OscConfig(&RCC_OscInitStruct) != HAL_OK)
    {
      Error_Handler();
    }
    g_hseFailed = 1U;                 /* 供启动日志上报 */
  }

  /** Initializes the CPU, AHB and APB buses clocks
  */
  RCC_ClkInitStruct.ClockType = RCC_CLOCKTYPE_HCLK|RCC_CLOCKTYPE_SYSCLK
                              |RCC_CLOCKTYPE_PCLK1|RCC_CLOCKTYPE_PCLK2;
  RCC_ClkInitStruct.SYSCLKSource = RCC_SYSCLKSOURCE_PLLCLK;
  RCC_ClkInitStruct.AHBCLKDivider = RCC_SYSCLK_DIV1;
  RCC_ClkInitStruct.APB1CLKDivider = RCC_HCLK_DIV1;
  RCC_ClkInitStruct.APB2CLKDivider = RCC_HCLK_DIV1;

  if (HAL_RCC_ClockConfig(&RCC_ClkInitStruct, FLASH_LATENCY_4) != HAL_OK)
  {
    Error_Handler();
  }
}

/* USER CODE BEGIN 4 */

/* USER CODE END 4 */

/**
  * @brief  This function is executed in case of error occurrence.
  * @retval None
  */
void Error_Handler(void)
{
  /* USER CODE BEGIN Error_Handler_Debug */
  /* User can add his own implementation to report the HAL error return state */
  /* 【2026-10-10 诊断】不再静默：先把现场打出来（走 TX 轮询，不依赖中断）。
   * 看到这一行就说明某个 HAL 调用返回了错误并被上层当成致命错误处理。
   *
   * 【2026-10-11 加固】重要教训：本函数**曾经是无限复位的根源**。
   *   链条：HAL_UART_Init() 失败 -> 其 MspDeInit 会关闭 USART1 时钟
   *        -> 本函数调用 BSP_UART_EmergencyPuts() 等 TC -> 时钟关了 TXE/TC
   *           永不置位 -> 卡死（此时连 __disable_irq 都还没执行到）
   *        -> 3 秒后 IWDG 复位 -> 无限重启，串口/LED 全无输出。
   *   现在 BSP_UART_EmergencyPuts 已改为**有界自旋**（见 bsp_uart.c），
   *   这里再补一层：**用 LED(PE0) 快速闪烁指示"进了 Error_Handler"**，
   *   这样即使串口完全不可用，也能用肉眼确认死锁位置。
   *   之后停机等待 IWDG 复位（保留原语义，便于现场反复观察 LED 快闪）。 */
  BSP_UART_EmergencyPuts("\r\n!! ERROR_HANDLER !! (a HAL call returned error)\r\n");
  __disable_irq();

  /* 既然进了致命错误，就用 LED 持续快闪打标（不依赖串口、不依赖中断）。
   * PE0 低电平点亮；这里纯 GPIO 写，时钟在最早期已由 main() 开头使能。 */
  for (;;)
  {
    GPIOE->BRR  = (1UL << 0);                     /* 亮 */
    for (volatile uint32_t d = 0U; d < 800000U; d++) { __NOP(); }
    GPIOE->BSRR = (1UL << 0);                     /* 灭 */
    for (volatile uint32_t d = 0U; d < 800000U; d++) { __NOP(); }
  }
  /* USER CODE END Error_Handler_Debug */
}

#ifdef  USE_FULL_ASSERT
/**
  * @brief  Reports the name of the source file and the source line number
  *         where the assert_param error has occurred.
  * @param  file: pointer to the source file name
  * @param  line: assert_param error line source number
  * @retval None
  */
void assert_failed(uint8_t *file, uint32_t line)
{
  /* USER CODE BEGIN 6 */
  /* User can add his own implementation to report the file name and line number,
     ex: printf("Wrong parameters value: file %s on line %d\r\n", file, line) */
  /* USER CODE END 6 */
}
#endif /* USE_FULL_ASSERT */
