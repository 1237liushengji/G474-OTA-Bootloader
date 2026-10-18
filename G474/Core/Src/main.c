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
#include "usb_cdc.h"            /* 【2026-10-12】USB CDC 虚拟串口（新主控制台） */
#include "console.h"
#include "isotp.h"
#include "uds.h"
#include "ota_agent.h"   /* OTA：刷写服务与状态管理 */
#include "ota_client.h"  /* 【2026-10-12】板A 板内 OTA 发起状态机 */
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

/* 【2026-10-11 临时诊断开关】1 = 跳过 CAN_SelfTest 自检体（仅喂狗）。
 * 【2026-10-12】复位循环元凶已定位并修复（FDCAN2_IT0 中断，见 can.c / hal_msp.c），
 * 恢复自检：回环自发自收是验证 CAN 链路完整性的第一道关口。 */
#define APP_SKIP_CAN_SELFTEST    0
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

/* 【2026-10-11 复位源取证】APP 启动次数累加器。
 * 放在 RAM 里（不复位清零），用 SWD 读它即可知道"从上次上电到现在被复位了几次"。
 * 配合 [rst] RCC_CSR 打印行，可判定复位源与复位频率。 */
volatile uint32_t g_bootCnt;

/* 【2026-10-11 复位源取证 v2】无竞态累加器。
 * ⚠️ 关键：这些变量必须放在**不初始化区(.noinit, UNINIT)**，
 * 否则 __main 的 ZI 清零会在每次复位后把它们清成 0，
 * 导致永远只能看到"最近一次"的值、无法累积（本会话已实测到这个坑）。
 * scatter 文件里已分配 RW_IRAM2 0x2001FF00 UNINIT 0x100 给 *(.noinit)。
 *   g_rstFlagsAcc : bit23..bit31 复位标志的"历史并集"
 *   g_rstTotal    : 累计 boot 次数（跨复位累积）
 * 依次类推。用 SWD 读这些变量即可无竞态判定复位源分布。 */
__attribute__((section(".noinit"))) volatile uint32_t g_bootCnt;
__attribute__((section(".noinit"))) volatile uint32_t g_rstFlagsAcc;
__attribute__((section(".noinit"))) volatile uint32_t g_rstPinCnt;
__attribute__((section(".noinit"))) volatile uint32_t g_rstPorCnt;
__attribute__((section(".noinit"))) volatile uint32_t g_rstBorCnt;
__attribute__((section(".noinit"))) volatile uint32_t g_rstSftCnt;
__attribute__((section(".noinit"))) volatile uint32_t g_rstIwdgCnt;
__attribute__((section(".noinit"))) volatile uint32_t g_rstWwdgCnt;

/* 【2026-10-18 清理】原"主循环停摆定位"instrumentation（g_loopStage /
 * g_loopStageSample / g_loopStageAge / g_loopCnt + [hb] 心跳行）已整段移除。
 * 该类脚手架在排障期很有用，但会持续污染串口日志并占用 RAM；
 * 若将来再次怀疑主循环卡死，可用 SWD 直接读 PC 或复检 `docs/13` 的方法。 */
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

  /* 【2026-10-18】OTA 传输期间让路：本函数走**软件 SPI**，一次刷新可占用
   * 数十毫秒；而本工程 CAN 主循环轮询、FDCAN 硬件 RX FIFO 只有 3 项，
   * 这段时间足以丢掉 ISO-TP 连续帧 → 整块作废重发。
   * 屏只是"心跳指示"，OTA 期间暂停完全不影响功能，且刷新计时不推进，
   * 传输结束后立即恢复。 */
  if (Ota_IsDownloading())
  {
    return;
  }

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

  /* 【2026-10-12】屏幕第 10..16 行显示滚动的最近 7 行日志。
   * 串口 RX 已确认不可用（板载 CH340K 走线开路），这块屏是当前唯一
   * 不依赖任何外部设备的可观测通道。 */
  BSP_UART_LcdLogRender();
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
/* 【2026-10-12】改回 1：CAN 发送问题已修复（实测 init_status=0、
 * NBTP=0x02000A03 正确写入、CAN_Init 仅耗时 4ms），且 CAN_Configure() 里
 * 本来就有 HAL_FDCAN_Start()（我曾误判它缺失并加过重复调用，已移除）。
 * 板间 OTA 联调需要周期性报文来确认链路是否建立。 */
#ifndef CAN_HEARTBEAT_ENABLE
#define CAN_HEARTBEAT_ENABLE 1
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

  /* ============ 【2026-10-12】启动 MCU 原生 USB 虚拟串口（新主控制台）========
   * 为什么改走 USB：板上 CH340K 的 TXD→PA10 走线开路（已实证），且 PC 每次
   * 打开/写该串口都会经 RTS→NRST 复位整板。原生 USB（PA11/PA12，"USB设备"口）
   * 完全没有这两个问题。USART1 的 TX 仍保留作旁路日志。
   * 注意：本调用不阻塞、不等待枚举；枚举结果由 [usb] cfg=? 观察。
   * ========================================================================= */
  /* 【2026-10-12】USB CDC 暂不启用：经典 USB 内核 ISR 路径仍有 MemManage 故障
   * （已二分确认在 USB_LP_IRQHandler 链路）。代码已全部就绪，
   * 故障定位后把下面这行去掉注释即可。 */
  /* UsbCdc_Init(); */

  /* ============ 【2026-10-11 复位源取证】必须紧跟 BSP_UART_Init ============
   * 为什么要在这里做：
   *   PC 侧无法清零 RCC_CSR（CubeProgrammer 的 -w32 只能写 Flash），
   *   只有固件能"读取并清零"。把值打出来即可判定上一次复位到底是
   *   PINRSTF（RTS→NRST，板载 CH340K 一键下载电路）还是别的来源。
   * 放在 BSP_UART_Init 之后：确保打印通路已经就绪。
   * ====================================================================== */
  {
    uint32_t csr = RCC->CSR;                        /* 读，不清零 */
    /* ⚠️【2026-10-11 重要纠正】RCC_CSR 的位定义此前被我标错了一整排！
     * 以 CMSIS stm32g474xx.h 为准（已逐条核对 RCC_CSR_xxxRSTF_Pos）：
     *   bit25=OBLRSTF  bit26=PINRSTF  bit27=BORRSTF  bit28=SFTRSTF
     *   bit29=IWDGRSTF bit30=WWDGRSTF bit31=LPWRRSTF  bit1=LSIRDY  bit23=RMVF
     * 之前的错误映射导致把 IWDGRSTF(bit29) 误读成"欠压 BORRSTF"，
     * 从而把一个"看门狗复位"误判为"电源问题"——教训：位定义必须查头文件，
     * 不能凭记忆。 */
    BSP_UART_Printf("[rst] RCC_CSR=%08lX PINRST=%lu BORRST=%lu SFTRST=%lu "
                    "IWDGRST=%lu WWDGRST=%lu LPWRRST=%lu OBLRST=%lu (b25..b31)\r\n",
                    (unsigned long)csr,
                    (unsigned long)((csr >> 26) & 1U),   /* PINRSTF  b26 */
                    (unsigned long)((csr >> 27) & 1U),   /* BORRSTF  b27 */
                    (unsigned long)((csr >> 28) & 1U),   /* SFTRSTF  b28 */
                    (unsigned long)((csr >> 29) & 1U),   /* IWDGRSTF b29 */
                    (unsigned long)((csr >> 30) & 1U),   /* WWDGRSTF b30 */
                    (unsigned long)((csr >> 31) & 1U),   /* LPWRRSTF b31 */
                    (unsigned long)((csr >> 25) & 1U));  /* OBLRSTF  b25 */

    g_rstFlagsAcc |= (csr & 0xFE000000UL);   /* bit25..bit31 复位标志区 */

    /* 逐源计数器（位定义同上，已核对） */
    if (csr & (1UL << 26)) { g_rstPinCnt++;   }   /* PINRSTF  b26 */
    if (csr & (1UL << 27)) { g_rstBorCnt++;   }   /* BORRSTF  b27 */
    if (csr & (1UL << 28)) { g_rstSftCnt++;   }   /* SFTRSTF  b28 */
    if (csr & (1UL << 29)) { g_rstIwdgCnt++;  }   /* IWDGRSTF b29 */
    if (csr & (1UL << 30)) { g_rstWwdgCnt++;  }   /* WWDGRSTF b30 */

    RCC->CSR |= RCC_CSR_RMVF;                       /* 清标志，供下次判定 */
    g_bootCnt++;
    BSP_UART_Printf("[rst] g_bootCnt=%lu acc=%08lX pin=%lu por=%lu bor=%lu "
                    "sft=%lu iwdg=%lu wwdg=%lu\r\n",
                    (unsigned long)g_bootCnt, (unsigned long)g_rstFlagsAcc,
                    (unsigned long)g_rstPinCnt,  (unsigned long)g_rstPorCnt,
                    (unsigned long)g_rstBorCnt,  (unsigned long)g_rstSftCnt,
                    (unsigned long)g_rstIwdgCnt, (unsigned long)g_rstWwdgCnt);
  }

  /* ============ 【2026-10-11 RX 自环自检】验证 RX 通路是否物理完好 ========
   * 背景：PC 侧每次 write() 都会经 RTS→NRST 复位本板，导致外部发来的字节
   *       被复位清掉，rxIrq 永远为 0 —— 无法判断 RX 通路本身好不好。
   * 办法：用 USART1 自己的"接收器请求位"RQR.RXFRQ 在**片内**造一个
   *       接收事件（不依赖 PA10 任何外部信号）：
   *         1) 置 RQR.RXFRQ：清 RXNE/FE/ORE/PE，并让接收移位器重新同步
   *         2) 直接把一个字节写进 RDR（模拟"收到一个字节"的等效效果）
   *       然后看 RXNE 是否置位、把 RDR 读回是否等于所写值。
   *       —— 这能证明"USART1 接收通路 + RXNE + NVIC 中断"这条链是通的。
   * 判读：
   *   rxPath=OK   ⟹ USART1 接收侧软/硬件链路完好，问题 100% 在 PA10 外部走线
   *   rxPath=FAIL ⟹ USART1 接收侧本身异常（需查时钟/复位/内核）
   * 注：这段是纯寄存器操作，绕开 HAL，不受复位影响；放最前面跑。
   * ====================================================================== */
  {
    uint32_t t;

    /* 关掉接收中断，避免自检过程被打断 */
    USART1->CR1 &= ~USART_CR1_RXNEIE;

    /* 1) 请求清接收标志（RXFRQ）——同时清除 RXNE/FE/ORE/PE */
    USART1->RQR = USART_RQR_RXFRQ;

    /* 2) 直接读一次 RDR，确保状态干净 */
    (void)USART1->RDR;

    /* 3) 判定链路：ISE(空闲状态) 应为 1（空闲线），RXNE 应为 0 */
    t = HAL_GetTick();
    while ((HAL_GetTick() - t) < 2U) { __NOP(); }
    uint32_t isr1 = USART1->ISR;
    BSP_UART_Printf("[rxchk] ISR(before)=%08lX RXNE=%lu IDLE=%lu ORE=%lu FE=%lu\r\n",
                    (unsigned long)isr1,
                    (unsigned long)((isr1 >> 5) & 1U),
                    (unsigned long)((isr1 >> 4) & 1U),
                    (unsigned long)((isr1 >> 3) & 1U),
                    (unsigned long)((isr1 >> 1) & 1U));

    /* 4) 置 RE 确保接收使能，再用 RXFRQ 清一次 */
    USART1->CR1 |= USART_CR1_RE;
    USART1->RQR = USART_RQR_RXFRQ;
    t = HAL_GetTick();
    while ((HAL_GetTick() - t) < 2U) { __NOP(); }
    uint32_t isr2 = USART1->ISR;
    BSP_UART_Printf("[rxchk] ISR(after RXFRQ)=%08lX RXNE=%lu  RE=%lu UE=%lu\r\n",
                    (unsigned long)isr2,
                    (unsigned long)((isr2 >> 5) & 1U),
                    (unsigned long)((USART1->CR1 >> 2) & 1U),
                    (unsigned long)((USART1->CR1 >> 0) & 1U));

    /* 5) 恢复挂起的接收中断（对应 BSP_UART_Init 里的 HAL_UART_Receive_IT） */
    USART1->CR1 |= USART_CR1_RXNEIE;
    BSP_UART_Send("[rxchk] RX path self-check done\r\n", 33U);
  }

  /* ========== 【2026-10-11 决定性判据】PA10 内部下拉探测 ================
   * 这是本会话最可靠的一条判据：只看**静态电平**，不受采样窗口影响。
   *
   *   串口空闲态，CH340K 的 TXD 是推挽输出、驱动为高。
   *   MCU 内部上下拉约 40kΩ，远弱于对方推挽。
   *   把 PA10 临时改成"输入+下拉"后读电平：
   *
   *     PA10pd = 1  ⟹ 外部强驱动把它拉住 ⟹ **TXD 与 PA10 连通**（RX 通路物理OK）
   *     PA10pd = 0  ⟹ 内部下拉把它拉低   ⟹ **外部无驱动 ⟹ 走线开路**
   *
   * 对照：原配置（复用+内部上拉）读数 pu **恒为 1**，两种情况无法区分 ——
   *       这正是此前多轮判断反复摇摆的根源。必须改下拉才能分辨。
   * ====================================================================== */
  /* ===== 【2026-10-11 最小化判据】直接写寄存器把 PA10 拉低，看板子是否复位 =====
   * 前面的 HAL 版本一进入就复位，无法区分是"HAL 重配置有问题"还是
   * "把 PA10 拉低这件事本身会复位整板"。这里绕开 HAL，只写两个寄存器，
   * 并逐步打点，把两种可能彻底分开。
   * 判读：
   *   打印到 [t1]  ⟹ 关 RX 中断无副作用
   *   打印到 [t2]  ⟹ 把 PA10 改成"输入"无副作用
   *   打印到 [t3]  ⟹ **把 PA10 拉低也无副作用**（则 HAL 版本的问题是别的）
   *   卡在 [t2]/[t3] 之间 ⟹ **拉低 PA10 会复位整块板子**
   *        ⟹ 强证据表明 PA10 并未接在 CH340K 的 TXD 上，
   *          而是接在某个复位/控制网络上（这能同时解释 RX 收不到 + 发送即复位）
   * ======================================================================== */
  {
    uint32_t m, p, pa10;
    uint32_t cr1_save, cr3_save;

    BSP_UART_Send("[t1] rxie off\r\n", 15U);
    /* 【关键修正】必须**同时**关掉 RE 和 EIE。
     * 之前只关了 RXNEIE，漏了 CR3.EIE（错误中断使能，实测 CR3=0x0001 是开的）。
     * 一旦把 PA10 从复用切走，USART 的 RX 输入就与引脚断开、变成悬空，
     * 会持续产生 FE/NE/ORE 错误；EIE=1 时这些错误会**不断触发中断**，
     * 把主循环彻底饿死 —— 现象看起来像"一改引脚就复位"，实际是
     * 3 秒后被 IWDG 复位（RCC_CSR 的 bit29=IWDGRSTF 证实）。 */
    cr1_save = USART1->CR1;
    cr3_save = USART1->CR3;
    USART1->CR1 &= ~(USART_CR1_RE | USART_CR1_RXNEIE);
    USART1->CR3 &= ~USART_CR3_EIE;

    /* PA10 -> 输入模式（上拉仍在，电平不变） */
    m = GPIOA->MODER;
    m &= ~(3UL << (10U * 2U));
    GPIOA->MODER = m;
    BSP_UART_Send("[t2] moder=input\r\n", 18U);

    /* PA10 -> 内部下拉（把引脚拉低） */
    p = GPIOA->PUPDR;
    p &= ~(3UL << (10U * 2U));
    p |=  (2UL << (10U * 2U));
    GPIOA->PUPDR = p;
    BSP_UART_Send("[t3] pupdr=pulldown\r\n", 21U);

    /* 等待电平稳定后读取 */
    {
      uint32_t t0 = HAL_GetTick();
      while ((HAL_GetTick() - t0) < 5U) { __NOP(); }
    }
    pa10 = (GPIOA->IDR & GPIO_PIN_10) ? 1U : 0U;
    BSP_UART_Printf("[t4] PA10(pulldown)=%lu  ==> %s\r\n",
                    (unsigned long)pa10,
                    (pa10 != 0U) ? "CONNECTED(外部驱动为高,RX通路物理连通)"
                                 : "OPEN(被内部下拉拉低,外部无驱动=走线开路)");

    /* 恢复：PA10 -> USART1_RX 复用 + 内部上拉；并恢复 USART 配置 */
    p = GPIOA->PUPDR;
    p &= ~(3UL << (10U * 2U));
    p |=  (1UL << (10U * 2U));          /* pull-up */
    GPIOA->PUPDR = p;
    m = GPIOA->MODER;
    m &= ~(3UL << (10U * 2U));
    m |=  (2UL << (10U * 2U));          /* AF 模式 */
    GPIOA->MODER = m;
    GPIOA->AFR[1] = (GPIOA->AFR[1] & ~(0xFUL << 8U)) | (7UL << 8U);  /* PA10 = AF7 */
    USART1->CR1 = cr1_save;
    USART1->CR3 = cr3_save;
    USART1->RQR = USART_RQR_RXFRQ;
    BSP_UART_Send("[t5] restored AF7+pullup\r\n", 25U);
  }

  /* ===== 【2026-10-11 全引脚扫描】找出"被外部推挽驱动"的引脚 ============
   * 判据（已由 [t1]~[t5] 在 PA10 上验证有效）：
   *   把引脚临时改成"输入 + 内部下拉(约40kΩ)"，等电平稳定后读 IDR。
   *     - 若该脚**外部浮空**          → 被 40k 下拉拉低 → 读到 0
   *     - 若该脚接**外部上拉/下拉电阻**→ 分压后仍低于阈值 → 读到 0
   *     - 若该脚被**推挽输出驱动为高** → 强驱动压过 40k → 读到 **1**
   *   因此"读到 1"的引脚 = 被外部芯片主动驱动的引脚。
   *
   * 目的：CH340K 的 TXD 是推挽输出且空闲为高。若它其实接到了**别的** MCU 引脚
   *      （而非 PA10），这次扫描就会把那个脚揪出来 —— 那么只要把 USART1_RX
   *      重映射/换用对应外设即可，**纯软件解决**。
   *      若扫完没有任何"意外被驱动"的脚 → 说明 TXD 根本没接到 MCU 上。
   *
   * 安全：只读 IDR，且每脚测完立刻恢复原 MODER/PUPDR；
   *      排除 PA13/PA14（SWD），避免丢失调试链路。
   * ===================================================================== */
  {
    GPIO_TypeDef        *ports[5] = { GPIOA, GPIOB, GPIOC, GPIOD, GPIOE };
    static const char    kName[5]  = { 'A', 'B', 'C', 'D', 'E' };
    /* 每个端口要排除的位（1=不测） */
    static const uint16_t kSkip[5] = {
      (uint16_t)((1U << 13) | (1U << 14)),   /* A: PA13/PA14 = SWD，绝不能动 */
      (uint16_t)(0U),                         /* B: 全部可测（含 485/CAN 脚，仅瞬时切换） */
      (uint16_t)(0U),                         /* C: 全部可测 */
      (uint16_t)(0U),                         /* D: 全部可测 */
      (uint16_t)(0U)                          /* E: 全部可测（PE0/PE1 = LED 作参考） */
    };
    uint32_t cr1_save = USART1->CR1;
    uint32_t cr3_save = USART1->CR3;
    uint16_t hi[5] = { 0U, 0U, 0U, 0U, 0U };  /* 读数为 1（被外部驱动）的位 */
    uint8_t  p;
    uint8_t  b;

    USART1->CR1 &= ~(USART_CR1_RE | USART_CR1_RXNEIE);   /* 防错误中断风暴 */
    USART1->CR3 &= ~USART_CR3_EIE;

    for (p = 0U; p < 5U; p++)
    {
      for (b = 0U; b < 16U; b++)
      {
        uint32_t pin  = (1UL << b);
        uint32_t moder, pupdr, t0;

        if ((kSkip[p] & (uint16_t)pin) != 0U)
        {
          continue;
        }

        moder = ports[p]->MODER;
        pupdr = ports[p]->PUPDR;

        /* 切成 输入 + 下拉 */
        ports[p]->MODER = (moder & ~(3UL << (b * 2U)));
        ports[p]->PUPDR = (pupdr & ~(3UL << (b * 2U))) | (2UL << (b * 2U));

        t0 = HAL_GetTick();
        while ((HAL_GetTick() - t0) < 2U) { __NOP(); }

        if ((ports[p]->IDR & pin) != 0U)
        {
          hi[p] |= (uint16_t)pin;
        }

        ports[p]->MODER = moder;               /* 立即恢复原配置 */
        ports[p]->PUPDR = pupdr;
      }
    }

    USART1->CR1 = cr1_save;
    USART1->CR3 = cr3_save;
    USART1->RQR = USART_RQR_RXFRQ;

    BSP_UART_Printf("[sweep] driven-high mask: A=%04X B=%04X C=%04X D=%04X E=%04X\r\n",
                    (unsigned)hi[0], (unsigned)hi[1], (unsigned)hi[2],
                    (unsigned)hi[3], (unsigned)hi[4]);

    /* 逐个列出被驱动的引脚，方便直接对照原理图 */
    for (p = 0U; p < 5U; p++)
    {
      if (hi[p] == 0U)
      {
        continue;
      }
      for (b = 0U; b < 16U; b++)
      {
        if ((hi[p] & (uint16_t)(1UL << b)) != 0U)
        {
          BSP_UART_Printf("[sweep]   %c%u is DRIVEN HIGH externally\r\n",
                          kName[p], (unsigned)b);
        }
      }
    }
  }

  /* ====== 【2026-10-11 RX 高频见证窗口】约 2 秒 PA10 边沿捕获 ============
   * 为什么要这一段：
   *   诊断任务(BSP_UART_DiagTask)的采样窗口只有 200ms、周期 3s，
   *   若 PC 只发少量字节，200ms 窗口很可能整段落在"没有数据"的间隙里
   *   → edges 采到 0，但**不能**证明信号没到（观测偏差）。
   * 做法：这里做一次**连续约 2 秒、无中断、纯轮询**的 PA10 采样，
   *   统计跳变次数与最终电平。PC 侧只要在这 2 秒内持续发送即可命中。
   * 附加价值：把 TX(PA9) 的 IDR 也一起采，用于证明"PA9 确实在动"，
   *   从而把"TX 通路"与"RX 通路"分开判定。
   * 判读：
   *   PA10edges > 0  ⟹ 信号**物理到达了 PA10**，问题在 USART 采样/配置
   *   PA10edges = 0 且 PA9edges > 0 ⟹ PA9(TX) 在动、PA10 没收货
   *                                     ⟹ **RX 外部走线/CH340K RXD 侧断**
   * ==================================================================== */
  {
    uint32_t edgesRx = 0U, edgesTx = 0U;
    uint32_t prevRx, prevTx, cur;
    uint32_t t0 = HAL_GetTick();
    uint32_t src = 0U;

    prevRx = (GPIOA->IDR & GPIO_PIN_10) ? 1U : 0U;
    prevTx = (GPIOA->IDR & GPIO_PIN_9)  ? 1U : 0U;

    /* 约 2 秒；同时记录 RCC_CSR 是否被复位置位（判断期间有没有复位） */
    while ((HAL_GetTick() - t0) < 200U)   /* 【2026-10-12】原 2000ms，结论已定，缩到 200ms 省启动时间 */
    {
      cur = GPIOA->IDR;
      if (((cur >> 10) & 1U) != prevRx) { edgesRx++; prevRx = (cur >> 10) & 1U; }
      if (((cur >> 9)  & 1U) != prevTx) { edgesTx++; prevTx = (cur >> 9)  & 1U; }
    }

    BSP_UART_Printf("[rxwin] 2s: PA10edges=%lu PA9edges=%lu  PA10=%lu PA9=%lu IDR=%04lX\r\n",
                    (unsigned long)edgesRx, (unsigned long)edgesTx,
                    (unsigned long)prevRx, (unsigned long)prevTx,
                    (unsigned long)(GPIOA->IDR & 0xFFFFU));
    src = RCC->CSR;
    BSP_UART_Printf("[rxwin] RCC_CSR(now)=%08lX (若 reset 位置位=期间发生过复位)\r\n",
                    (unsigned long)src);
  }

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

    /* 【2026-10-11 IWDG 隔离实验】临时跳过自检体，直接走后续流程。
     * 目的：区分「复位是 CAN_SelfTest 引起」还是「启动流程别处喂狗不及时」。
     * 定位完成后必须改回 0。 */
#if (APP_SKIP_CAN_SELFTEST != 0)
    hiwdg.Instance = IWDG;
    (void)HAL_IWDG_Refresh(&hiwdg);
    BSP_UART_BootMark("D2: CAN_SelfTest SKIPPED (experiment)");
    st = false;
#else
    st = CAN_SelfTest();
#endif

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

  hiwdg.Instance = IWDG;
  (void)HAL_IWDG_Refresh(&hiwdg);
  BSP_UART_BootMark("F: before Uds/OtaTunnel/Ota init");
  Uds_Init();                                      /* ISO-TP+UDS诊断栈（角色自动分配） */
  OtaTunnel_Init();                                /* OTA：串口<->诊断CAN隧道（默认关闭） */
  Ota_Init();                                      /* OTA：读控制区/识别本槽/置试运行待确认 */
  (void)HAL_IWDG_Refresh(&hiwdg);
  BSP_UART_BootMark("G: after Uds/OtaTunnel/Ota init");

  /* ---- LCD 状态屏（LCD_Init 内含约 760ms 延时，前后各喂一次狗，避免出现喂狗空窗）---- */
  BSP_UART_BootMark("H: before LCD/IWDG block");
  hiwdg.Instance = IWDG;
  (void)HAL_IWDG_Refresh(&hiwdg);
  LCD_Init();                                      /* 背光开 + 清屏为黑，失败则全白/花屏 */
  BSP_UART_LcdLogEnable(1U);                      /* 【2026-10-12】启用 TFT 滚动日志（串口不可用后的唯一可观测通道） */
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
    /* 【2026-10-18 清理】排障期的"主循环心跳 + 阶段采样"脚手架已移除。
     * 它每 100ms 打一行 [hb]，并把每个任务的进入点写成全局变量供 SWD 观察；
     * 问题定位完毕后既占空间又污染日志（实测 [hb] 一秒钟十行）。
     * 同理，BSP_UART_RxPinPulse / PinMonitor / DiagTask 这三个
     * "串口接收链路自诊断"任务也已从主循环摘除 —— 它们每 6 秒集中打印
     * 约 300 字节，单次阻塞 20ms 以上，是 ISO-TP 连续帧丢失的主要来源之一。 */
    App_Process();                             /* CAN 收帧分发 / 链路调度 */
    UsbCdc_Task();                             /* USB CDC 发送推进（非阻塞） */
    Console_Task();                            /* 串口命令行控制台 */
    CAN_Task();                                /* 轮询收发 + bus-off/发送停滞恢复 */
    IsoTp_Task();                              /* ISO-TP：STmin 节拍 / 超时 */
    Uds_Task();                                /* UDS：S3 会话 / 客户端超时 */
    Ota_Task();                                /* OTA：试运行自检通过后落定 */
    LcdUi_Task();                              /* LCD：1 秒刷新（OTA 期间自动让路） */
    OtaClient_Task();                          /* 板A 的 OTA 发起状态机 */

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
