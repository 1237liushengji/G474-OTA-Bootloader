/**
  ******************************************************************************
  * @file    bsp_uart.c
  * @brief   控制台串口驱动实现
  *
  * 设计说明：
  *   1. 发送：阻塞发送（控制台为低速率人机交互，单行<100字节@115200约10ms，
  *      对1Hz的CAN业务无影响；若未来扩展高负载日志，再升级为中断/DMA发送）；
  *   2. 接收：单字节中断接收 + 128字节环形缓冲（单生产者中断/单消费者主循环，
  *      无锁安全），控制台任务轮询取走；
  *   3. 缓冲满时丢弃新字节并统计（保底不阻塞中断）。
  ******************************************************************************
  */
#include "bsp_uart.h"
#include "usb_cdc.h"          /* 【2026-10-12】USB CDC 虚拟串口（当前默认关闭，见下） */
#include "bsp_lcd.h"         /* 【2026-10-12】TFT 滚动日志 */
#include "can.h"             /* 【2026-10-18】UART 等待期间顺带泵 CAN 接收 */
#include <stdarg.h>
#include <stdio.h>
#include <string.h>

/* ★★【2026-10-18】TFT 滚动日志镜像开关。
 *   这段代码曾把**每一条**串口输出同时刷到软 SPI 的 TFT 上，是主循环里
 *   最长的一段阻塞（软 SPI + 滚动重绘，单条数十毫秒级）。
 *   它当年是为了绕开"串口 RX 走线开路、抓不到日志"而加的临时观测通道；
 *   如今串口 TX 完全可靠（本工程所有取证都靠它），镜像只剩副作用：
 *   主循环被周期性卡住 → FDCAN 硬件 RX FIFO（HAL 固定 3 项）溢出
 *   → ISO-TP 连续帧丢失 → OTA 每块都要重发。
 *   故默认关闭；需要看屏时把它置 1 即可。 */
#ifndef BSP_UART_LCD_MIRROR
#define BSP_UART_LCD_MIRROR   0
#endif

UART_HandleTypeDef huart1;

/*--------------------------------------- 模块内部变量 --------------------------------------*/
static uint8_t  s_rxRing[BSP_CONSOLE_RX_RING_SIZE];   /* 接收环形缓冲 */
static volatile uint16_t s_rxHead;                    /* 写指针（仅中断修改） */
static volatile uint16_t s_rxTail;                    /* 读指针（仅主循环修改） */
static uint8_t  s_rxBuf;                              /* 单字节中断接收缓冲 */

/*------------------------------ 【2026-10-12】TFT 滚动日志 ---------------------------------
 * 目的：板载 CH340K 的 RX 走线已确认开路、且其 RTS→NRST 会复位整板
 *       （见 docs/15 §十四），串口作为调试通道已不可用。
 *       于是把"最后 N 行日志"镜像到板载 TFT 上 —— 现场无需任何串口即可看到状态。
 * 设计：
 *   - BSP_UART_Send() 在**发送的同时**把字符喂进这里的行累积器，遇 '\n' 落一行
 *     （不在这里刷屏：SPI 刷屏慢，会拖慢主循环）。
 *   - 刷屏由 BSP_UART_LcdLogRender() 在 LcdUi_Task() 里按 1Hz 做。
 * 注意：非 ASCII（含中文）的字节会被替换成 '.'，因为字库只覆盖 ASCII。 */
#define BSP_LCDLOG_LINES  7U                          /* 屏幕第 10..16 行 */
#define BSP_LCDLOG_COLS   20U                          /* LCD_CHAR_COLS = 20 */

static char           s_lcdLog[BSP_LCDLOG_LINES][BSP_LCDLOG_COLS + 1U];
static uint8_t        s_lcdLogHead;                   /* 下一个写入的行号（环形） */
static uint8_t        s_lcdLogLen;                    /* 当前行已累积的字符数 */
static uint8_t        s_lcdLogTotal;                  /* 累计行数（用于判断"有无新行"） */
static uint8_t        s_lcdLogReady;                  /* LCD_Init 之后才允许捕获 */
static char           s_lcdLine[BSP_LCDLOG_COLS + 1U];/* 行累积器 */

/*--------------------------------------- 模块内部函数 --------------------------------------*/
/**
  * @brief  把一段 ASCII 文本按行喂进 TFT 日志环形缓冲（不刷屏）
  */
static void Bsp_LcdLog_Feed(const char *data, uint16_t len)
{
  uint16_t i;

  if (s_lcdLogReady == 0U)
  {
    return;                                 /* LCD 还没初始化，先不记 */
  }

  for (i = 0U; i < len; i++)
  {
    char c = data[i];

    if (c == '\r')
    {
      continue;                            /* 忽略 CR，统一按 LF 收行 */
    }

    if (c == '\n')
    {
      s_lcdLine[s_lcdLogLen] = '\0';
      /* 覆盖最旧的一行 */
      (void)snprintf(s_lcdLog[s_lcdLogHead], BSP_LCDLOG_COLS + 1U, "%s", s_lcdLine);
      s_lcdLogHead = (uint8_t)((s_lcdLogHead + 1U) % BSP_LCDLOG_LINES);
      if (s_lcdLogTotal < 255U) { s_lcdLogTotal++; }
      s_lcdLogLen = 0U;
      continue;
    }

    /* 非可打印字符（含中文 UTF-8 多字节）一律替换，避免出现乱码方块 */
    if ((c < 0x20) || (c > 0x7E))
    {
      c = '.';
    }

    if (s_lcdLogLen < BSP_LCDLOG_COLS)
    {
      s_lcdLine[s_lcdLogLen++] = c;
    }
    else
    {
      s_lcdLogLen = 0U;                    /* 超长行丢弃，等下一个 LF */
    }
  }
}

void BSP_UART_LcdLogEnable(uint8_t enable)
{
  s_lcdLogReady = enable;
}

void BSP_UART_LcdLogRender(void)
{
  uint8_t row, i, start;

  if (s_lcdLogReady == 0U)
  {
    return;
  }

  start = (uint8_t)((s_lcdLogHead + BSP_LCDLOG_LINES - BSP_LCDLOG_LINES) % BSP_LCDLOG_LINES);
  for (row = 0U; row < BSP_LCDLOG_LINES; row++)
  {
    i = (uint8_t)((start + row) % BSP_LCDLOG_LINES);
    LCD_PrintField((uint8_t)(10U + row), 0, s_lcdLog[i], BSP_LCDLOG_COLS,
                   LCD_COLOR_GREEN, LCD_COLOR_BLACK);
  }
}

uint8_t BSP_UART_LcdLogTotal(void)
{
  return s_lcdLogTotal;
}

/* 【2026-10-09 真机诊断】接收链路诊断计数器。上位机 TUN ON 收不到应答，
 * 而"设备能发、PC 发什么都不回"说明是**接收**方向断。二者必有一个坏：
 *   - s_rxIrqCnt==0  -> UART 中断根本没进（NVIC/USART 未就绪，或 PA10 接线/复用有问题）
 *   - s_rxIrqCnt>0  -> 中断在跑，问题在别处（行解析/命令匹配）
 * 挂在 stats 命令里打印，纯诊断、不影响功能，验证完可保留。 */
static volatile uint32_t s_rxIrqCnt;                  /* UART Rx 中断触发次数 */
/* 【2026-10-11】PA10 引脚跳变的"累计"计数（由主循环高频调用的
 * BSP_UART_RxPinPulse() 维护）。这是判断"信号是否真的到达引脚"的
 * 无观测偏差判据 —— 只要引脚动过一次就一定 > 0。 */
static volatile uint32_t s_rxPinPulseCnt;
static volatile uint32_t s_rxPinLevel;
static volatile uint32_t s_rxDropCnt;                 /* 环形缓冲满被丢弃的字节数 */

/* 【2026-10-11】全端口引脚跳变监视器（见 BSP_UART_PinMonitor 注释）。
 * 目的：在"PC 发送期间 MCU 仍然存活"的重叠窗口内，找出到底是哪个引脚
 * 在被外部器件（CH340K 的 TXD）驱动 —— 从而判断能否用纯软件重映射解决。 */
static uint16_t s_pinMonLast[BSP_PINMON_PORT_NUM];            /* 上次 IDR 快照 */
static volatile uint16_t s_pinMonEver[BSP_PINMON_PORT_NUM];   /* 曾经跳变过的位 */
static volatile uint32_t s_pinMonCnt[BSP_PINMON_PORT_NUM][16];/* 逐位跳变计数 */
static uint8_t  s_pinMonInit;
/* 【2026-10-11】PA10 下拉探测结果：1=外部驱动为高(连通) 0=被下拉拉低(开路) */
static uint8_t  s_rxProbePullDown;
/* 【2026-10-11】PA10 下拉探测的原始读数（含恢复后的上拉读数，用于对比） */
static uint8_t  s_rxProbePullUp;
/* 【2026-10-11】分步探测用：步骤 A(输入+上拉)的读数，以及已完成到第几步。
 * 为什么需要：整段探测会复位板子，必须知道死在 A 步还是 B 步 ——
 * 这决定了"是 GPIO 重配置有害"还是"把 PA10 拉低会复位整板"。 */
static uint8_t  s_rxProbeStepA;
static uint8_t  s_rxProbeStage;

/**
  * @brief  取 UART 接收中断触发次数（诊断用，见 Cmd_Stats）
  */
uint32_t BSP_UART_RxIrqCount(void)
{
  return s_rxIrqCnt;
}

/**
  * @brief  取因环形缓冲满而被丢弃的字节数（诊断用）
  */
uint32_t BSP_UART_RxDropCount(void)
{
  return s_rxDropCnt;
}

/**
  * @brief  打印异常栈帧里的 fault PC / LR / xPSR —— 直接定位"是哪条指令出的错"
  *
  * 【为什么需要它】
  *   只看 CFSR/HFSR 常常看不出问题：本次排查 USB CDC 时的 MemManage 故障，
  *   CFSR 与 HFSR **都是 0**，光看它们完全无从下手。真正有用的是异常栈帧里
  *   被硬件压入的 PC —— 它就是触发异常的那条指令。
  *
  * 【栈帧布局（Cortex-M 异常入栈，8 个字）】
  *   [0]=R0  [1]=R1  [2]=R2  [3]=R3  [4]=R12  [5]=LR  [6]=PC  [7]=xPSR
  *
  * 【为什么要"逐偏移搜索"】
  *   本函数被调用时，处理程序已经压过若干 callee-saved 寄存器，
  *   所以传入的 sp 只比真实帧底低几个字节，无法直接假定偏移 0。
  *   判据用 xPSR：异常帧里的 xPSR 其高字节恒为 0x01（Thumb 位=1 且不带
  *   IT/异常号位），与栈上残留数据区分度很高。逐个 4 字节试，找到第一个
  *   满足的就认为命中了帧。
  *
  * @param  sp 处理程序入口处取得的栈指针（由调用方在**未调用任何函数前**取）
  */
void BSP_UART_PrintExcFrame(uint32_t sp)
{
  uint32_t off;

  /* 先做范围自检：sp 若明显不是 RAM，直接放弃，避免二次故障 */
  if ((sp < 0x20000000UL) || (sp > 0x20020000UL))
  {
    BSP_UART_Send("  excFrame: bad sp\r\n", 22U);
    return;
  }

  for (off = 0U; off <= 32U; off += 4U)
  {
    const volatile uint32_t *f = (const volatile uint32_t *)(sp + off);
    uint32_t pc  = f[6];
    uint32_t xpsr = f[7];

    if ((xpsr & 0xFF000000UL) == 0x01000000UL)
    {
      BSP_UART_EmergencyHex("  excPC  = ", pc);
      BSP_UART_EmergencyHex("  excLR  = ", f[5]);
      BSP_UART_EmergencyHex("  excPSR = ", xpsr);
      BSP_UART_EmergencyHex("  excOff = ", off);
      return;
    }
  }

  BSP_UART_Send("  excFrame: not found\r\n", 26U);
}

/**
  * @brief  接收链路自诊断：RX 完全收不到字节时，周期性把 USART1/GPIO/NVIC 的
  *         真实寄存器状态打出来（走已知可用的 TX 通路），用于区分
  *         "信号没到引脚" 与 "信号到了但 USART/中断没处理"。
  *
  * 【2026-10-10 新增】起因：真机上 TX 正常（banner 能打出），但 PC 发任何字符
  * 都零响应，连字符回显都没有。静态代码逐行核对全部正确（引脚 PA9/PA10+AF7、
  * huart1 单一定义、ISR 接线、NVIC 使能），所以必须靠**运行态测量**定位：
  *
  *   判据（PC 侧持续发送时看本 dump）：
  *     a) PA10 edges/20ms > 0，但 rxIrq 恒 0   -> 信号到了引脚，USART/中断配置问题
  *     b) PA10 edges/20ms == 0                 -> 信号根本没到引脚（接线/适配器问题）
  *     c) MODER 的 PA9/PA10 字段不是 0b10      -> 引脚被别处改写成非复用模式
  *     d) CR1 缺 RE 或 RXNEIE                  -> Receive_IT 未生效
  *     e) ISER1 bit5 == 0                      -> USART1_IRQn 未使能
  *
  * 仅在 s_rxIrqCnt==0（从未收到过）时打印，最多 30 次（约 90 秒），避免刷屏。
  */
/**
  * @brief  应急输出：直接写 USART1 寄存器发送一行文本。
  *
  * 【2026-10-10 新增】用于 HardFault_Handler / Error_Handler / 启动打点这类
  * "HAL 可能已不可用"的场景：不经过 HAL_UART_Transmit（它先检查 gState，
  * 状态不对会直接返回 HAL_BUSY 而什么都不发），也不依赖任何中断。
  *
  * 安全护栏：先检查 CR1 的 UE|TE 位；UART 尚未初始化时**直接放弃输出**，
  * 避免在"等 TXE"上死等（启动早期故障会走到这里）。
  */
void BSP_UART_EmergencyPuts(const char *s)
{
  if (s == NULL)
  {
    return;
  }
  /* UE = CR1.bit0，TE = CR1.bit3；未使能则无法发送，直接返回 */
  if ((USART1->CR1 & 0x00000009U) != 0x00000009U)
  {
    return;
  }

  /* 【2026-10-11 关键修复】原来的"等 TXE/TC"是**无限循环**，一旦 USART1
   * 的内核时钟被关（典型场景：HAL_UART_Init() 失败时 HAL_UART_MspDeInit()
   * 会 __HAL_RCC_USART1_CLK_DISABLE()），TXE/TC 永远不置位 -> 卡死。
   * 而本函数恰恰是从 Error_Handler() 里调用的 —— 于是形成
   *   HAL_UART_Init 失败 -> Error_Handler -> EmergencyPuts 等 TC 卡死
   *   -> 3 秒后 IWDG 复位 -> 无限重启，且一条日志都打不出来。
   * 现在改成**有界自旋**：等不到就直接放弃本行输出，绝不阻塞。
   * 采样次数取 ~200000（150MHz 下约几毫秒），足够 115200 发出 1~5 字节。 */
  while (*s != '\0')
  {
    uint32_t guard = 200000U;

    /* ★【2026-10-18】趁等 TXE 的空档把 FDCAN 接收 FIFO 排空。
     *   本工程的 CAN 是主循环轮询、且 HAL 把硬件 RX FIFO0 固定为 3 项，
     *   而 UART 打印（115200，每字节 ~87µs）是 CF 流期间最长的阻塞段
     *   （周期诊断一次 300 字节 ≈ 26ms，可吞掉十几个 ISO-TP 连续帧）。
     *   在这里泵一次，等于把打印期间到达的 CAN 帧全部安全落到 16 项环形缓冲，
     *   从根上消除"传几块就丢一帧、整块作废重发"的现象。
     *   CAN 尚未初始化时该函数内部自行返回，不影响早期启动日志。 */
    CAN_PumpRx();

    while (((USART1->ISR & 0x00000080U) == 0U) && (guard != 0U))   /* 等 TXE（有界） */
    {
      guard--;
    }
    if (guard == 0U)
    {
      return;                                    /* 时钟/外设异常：放弃输出，绝不卡死 */
    }
    USART1->TDR = (uint32_t)(uint8_t)(*s);
    s++;
  }

  {
    uint32_t guard = 200000U;

    while (((USART1->ISR & 0x00000040U) == 0U) && (guard != 0U))   /* 等 TC（有界） */
    {
      guard--;
    }
  }
}

/**
  * @brief  应急输出一个 32 位十六进制值（形如 "TAG=XXXXXXXX\r\n"）
  */
void BSP_UART_EmergencyHex(const char *tag, uint32_t value)
{
  static const char kHexDigits[] = "0123456789ABCDEF";
  char              buf[9];
  int               i;

  for (i = 0; i < 8; i++)
  {
    buf[i] = kHexDigits[(value >> ((7 - i) * 4)) & 0x0FU];
  }
  buf[8] = '\0';

  BSP_UART_EmergencyPuts(tag);
  BSP_UART_EmergencyPuts(buf);
  BSP_UART_EmergencyPuts("\r\n");
}

/**
  * @brief  启动检查点：打印标记 + 当前 tick（十六进制）
  *
  * 【2026-10-10 新增】真机现象：APP 打印到 "main: led-bsrr-state M3" 后即被
  * IWDG 每 3 秒复位，主循环从未跑起来（因此串口 RX 看起来"完全无响应"——
  * Console_Task 根本没执行）。用这些检查点把卡点逐步缩小到一个函数内，
  * 同时用 tick 值判断 SysTick 是否在推进（tick 冻结会让 HAL 里所有
  * `while ((HAL_GetTick()-start) > TIMEOUT)` 变成死循环）。
  */
void BSP_UART_BootMark(const char *msg)
{  static const char kHexDigits[] = "0123456789ABCDEF";
  uint32_t          t = HAL_GetTick();
  char              buf[9];
  int               i;

  for (i = 0; i < 8; i++)
  {
    buf[i] = kHexDigits[(t >> ((7 - i) * 4)) & 0x0FU];
  }
  buf[8] = '\0';

  BSP_UART_EmergencyPuts("[boot] ");
  BSP_UART_EmergencyPuts(msg);
  BSP_UART_EmergencyPuts(" tick=0x");
  BSP_UART_EmergencyPuts(buf);
  BSP_UART_EmergencyPuts("\r\n");
}

/**
  * @brief  故障现场快照（供 HardFault / MemManage / BusFault / UsageFault 共用）
  *
  * 【2026-10-10 新增】it.c 里的 MemManage/BusFault/UsageFault 三个处理函数原本都是
  * **静默 `while(1)`**，故障一旦落到它们头上就只剩"约 3 秒后被 IWDG 复位"，
  * 零线索。本函数只读内核寄存器、不访问栈帧，因此不会引发二次故障。
  */
void BSP_UART_FaultDump(const char *tag)
{
  BSP_UART_EmergencyPuts("\r\n!! ");
  BSP_UART_EmergencyPuts(tag);
  BSP_UART_EmergencyPuts(" !!\r\n");
  BSP_UART_EmergencyHex("  CFSR  = ", SCB->CFSR);
  BSP_UART_EmergencyHex("  HFSR  = ", SCB->HFSR);
  BSP_UART_EmergencyHex("  MMFAR = ", SCB->MMFAR);
  BSP_UART_EmergencyHex("  BFAR  = ", SCB->BFAR);
  BSP_UART_EmergencyHex("  MSP   = ", __get_MSP());
  BSP_UART_EmergencyHex("  PSP   = ", __get_PSP());
}

void BSP_UART_DiagTask(void)
{
  static uint32_t s_next = 0U;
  static uint32_t s_cnt  = 0U;
  static uint32_t s_lastEdgesTx = 0U;             /* 【2026-10-11】PA9 边沿数（TX 见证） */
  uint32_t        now;
  uint32_t        edges = 0U;
  uint32_t        prev;
  uint32_t        t0;

  /* 【2026-10-11 实验】去掉 30 次上限，改为"收到第一个字节才停"（s_rxIrqCnt!=0）。
   * 原因：诊断行只在开机后 90s 内出现，导致后续任何时候打开串口都看不到状态。
   * 保留 s_rxIrqCnt!=0 的停止条件 —— 一旦 RX 通了就停，不刷屏。 */
  if (s_rxIrqCnt != 0U)
  {
    return;
  }

  now = HAL_GetTick();
  if ((s_next != 0U) && ((int32_t)(now - s_next) < 0))
  {
    return;
  }
  /* 【2026-10-11 实验】首次打印提前到 +800ms。
   * 原因：PC 侧"打开/发送"会复位板子，每次复位后的**可用观测窗口只有约 3s**
   * （实测复位周期 ~3.2s）。若首次打印等到 +3000ms，很可能与下一次复位撞车，
   * 于整个发送期间一条诊断都抓不到 → 观测不到就等于没测。
   * 提前到 800ms 可确保在"MCU 已启动 + PC 仍在发送"的重叠窗口内拿到数据。 */
  s_next = now + ((s_cnt == 0U) ? 800U : 3000U);
  s_cnt++;

  /* 【2026-10-11 实验·加强版 v3】采样窗口 500ms，同时采 PA9(TX)。
   * 【关键新增】另有一个在**主循环每次调用**里运行的"累计边沿计数器"
   *   BSP_UART_RxPinPulse()（见下），它统计的是**从开机至今** PA10 上
   *   出现过的所有跳变。这彻底消除了"采样窗口恰好落在间隙"的观测偏差：
   *   只要 RX 线上曾经有过任何一个电平跳变，该计数就一定 > 0。
   */
  {
    uint32_t edgesTx = 0U;
    uint32_t prevTx  = (GPIOA->IDR & BSP_CONSOLE_TX_PIN) ? 1U : 0U;

    prev = (GPIOA->IDR & BSP_CONSOLE_RX_PIN) ? 1U : 0U;
    t0   = HAL_GetTick();
    while ((HAL_GetTick() - t0) < 500U)
    {
      uint32_t idr = GPIOA->IDR;
      uint32_t curRx = (idr & BSP_CONSOLE_RX_PIN) ? 1U : 0U;
      uint32_t curTx = (idr & BSP_CONSOLE_TX_PIN) ? 1U : 0U;
      if (curRx != prev)   { edges++;   prev   = curRx; }
      if (curTx != prevTx) { edgesTx++; prevTx = curTx; }

      /* 【2026-10-11 实验】在这段 500ms 的"紧循环"里也采全端口。
       * 为什么必须加：
       *   BSP_UART_PinMonitor() 原本只在主循环快路径被调用，而诊断任务
       *   自身打印/采样期间不会经过主循环 —— 实测导致 `[pins] A=0000`
       *   （PA9 明明在打印却记不到，属观测盲区）。
       *   在紧循环里补采，可把 500ms 内**所有**端口的跳变完整捕获，
       *   这是本实验灵敏度最高的采样点。 */
      BSP_UART_PinMonitor();
    }
    s_lastEdgesTx = edgesTx;                        /* 供下方打印（避免重复采样） */
  }

  /* CR1 期望值 0x002D = UE|TE|RE|RXNEIE（bit0/3/2/5）；BRR 实测 0x0516 @115200/16x */
  BSP_UART_Printf("[uart] CR1=%04lX CR3=%04lX BRR=%04lX ISR=%08lX\r\n",
                  (unsigned long)USART1->CR1, (unsigned long)USART1->CR3,
                  (unsigned long)USART1->BRR, (unsigned long)USART1->ISR);
  BSP_UART_Printf("[uart] MODER=%08lX IDR=%04lX PA10=%lu PA9=%lu rxE/500ms=%lu txE/500ms=%lu\r\n",
                  (unsigned long)GPIOA->MODER, (unsigned long)(GPIOA->IDR & 0xFFFFU),
                  (unsigned long)((GPIOA->IDR & BSP_CONSOLE_RX_PIN) ? 1U : 0U),
                  (unsigned long)((GPIOA->IDR & BSP_CONSOLE_TX_PIN) ? 1U : 0U),
                  (unsigned long)edges, (unsigned long)s_lastEdgesTx);
  /* 【2026-10-11】累计跳变数 —— 无观测偏差的 RX 判据 */
  BSP_UART_Printf("[uart] PA10pulseTotal=%lu (自开机累计, >0 = 信号到过引脚)\r\n",
                  (unsigned long)s_rxPinPulseCnt);
  /* ISER1 bit5 = USART1_IRQn(37) 使能位 */
  BSP_UART_Printf("[uart] ISER1=%08lX rxIrq=%lu drop=%lu head=%u tail=%u\r\n",
                  (unsigned long)NVIC->ISER[1], (unsigned long)s_rxIrqCnt,
                  (unsigned long)s_rxDropCnt, (unsigned)s_rxHead, (unsigned)s_rxTail);
  BSP_UART_Printf("[uart] gState=%u RxState=%u RxCnt=%u\r\n",
                  (unsigned)huart1.gState, (unsigned)huart1.RxState,
                  (unsigned)huart1.RxXferCount);

  /* 【2026-10-11 灵敏度自检】上面连续打印了 6 行，PA9(TX) 必然产生大量跳变。
   * 在这里采一次，若随后 [pins] 的 A 掩码里出现 bit9，就证明**监视器能可靠
   * 捕获 115200 波特率的信号**；反之说明监视器有盲区，那么"没看到 PA10 跳变"
   * 就不能作为证据。这一步是让 RX 阴性结论成立的必要前提。 */
  BSP_UART_PinMonitor();

  /* 【2026-10-11】全端口跳变转储 —— 用于判断外部器件（CH340K TXD）落在哪个引脚。
   * 同时打印 tick：若两次转储之间 tick 突然回到 ~700，说明期间发生过复位，
   * 由此可判断"PC 长发送期间板子是否一直存活"（本实验成立的前提）。 */
  BSP_UART_Printf("[pins] t=%lu\r\n", (unsigned long)HAL_GetTick());
  BSP_UART_PinMonitorDump();

  /* ===== 【2026-10-11 终极判据】PA10"下拉 + 动态采样"测试 =============
   * 【2026-10-12 限流】结论已定（PA10 ↔ CH340K.TXD 走线开路）。本测试每次
   * 要占用约 153ms 且会临时夺走 USART1 的 RX 引脚，故只在开机后前 3 次
   * 诊断里跑，之后不再执行，避免长期浪费 CPU。
   * 若日后需要复查 RX 走线，把下面的 `s_cnt <= 3U` 改回 `1U`（恒真）即可。
   * 为什么这是终极判据：
   *   之前的"静态下拉"测试把 PA10 拉低后只读一次，若读数 0 就断言"外部没驱动"。
   *   但严格说，那只排除了"外部持续驱动为高"。若 CH340K 的 TXD 连着、而它
   *   当时恰好没在发送，推挽输出空闲应为高 —— 所以理论上仍应读到 1。
   *   为彻底消除争议，这里改成**动态采样**：
   *     1) 把 PA10 配成"输入 + 内部下拉"
   *     2) 在约 150ms 内高频采样 IDR，分别统计读到 1 / 0 的次数
   *     3) 期间 PC 若在发送，TXD 一定会在"字节间隙/停止位"回到高电平
   *   判读：
   *     high > 0  ⟹ 有外部**推挽驱动**把 PA10 拉高过 ⟹ **TXD 与 PA10 连通**
   *                  （内部 40k 下拉绝不可能自己变高 —— 只有外部强驱动能做到）
   *     high == 0 ⟹ 全程只能被自己的下拉拉住 ⟹ **外部无驱动，走线开路**
   *   注意：采样期间必须关掉 USART 的 RE/RXNEIE/EIE，否则切走引脚会让
   *         RX 悬空并产生错误中断风暴，把主循环饿死（3s 后被 IWDG 复位）。
   * ==================================================================== */
  if (s_cnt <= 3U)                                /* 【2026-10-12 限流】见上方说明 */
  {
    uint32_t cr1_save = USART1->CR1;
    uint32_t cr3_save = USART1->CR3;
    uint32_t moder_save = GPIOA->MODER;
    uint32_t pupdr_save = GPIOA->PUPDR;
    uint32_t t0;
    uint32_t high = 0U;
    uint32_t low  = 0U;

    USART1->CR1 &= ~(USART_CR1_RE | USART_CR1_RXNEIE);
    USART1->CR3 &= ~USART_CR3_EIE;

    /* PA10 -> 输入 + 下拉 */
    GPIOA->MODER = (moder_save & ~(3UL << 20));
    GPIOA->PUPDR = (pupdr_save & ~(3UL << 20)) | (2UL << 20);

    /* 【必须的稳定延时】切换前 PA10 被内部上拉拉在高电平，切到下拉后
     * 引脚电压要经过 RC 放电才降到低（40kΩ × 几十 pF ≈ 几 µs）。
     * 若不等待就立即采样，开头会读到一批"高"，被误判成"外部驱动过它"。
     * 实测：不加延时时 high=11/107万（正是这段放电暂态）。
     * 这里给 3ms（远大于放电时间），确保进入稳态后再统计。 */
    {
      uint32_t tw = HAL_GetTick();
      while ((HAL_GetTick() - tw) < 3U) { __NOP(); }
    }

    t0 = HAL_GetTick();
    while ((HAL_GetTick() - t0) < 150U)
    {
      if ((GPIOA->IDR & GPIO_PIN_10) != 0U) { high++; } else { low++; }
    }

    /* 恢复 */
    GPIOA->MODER = moder_save;
    GPIOA->PUPDR = pupdr_save;
    USART1->CR1 = cr1_save;
    USART1->CR3 = cr3_save;
    USART1->RQR = USART_RQR_RXFRQ;

    BSP_UART_Printf("[pdtest] PA10 w/ pulldown 150ms: high=%lu low=%lu ==> %s\r\n",
                    (unsigned long)high, (unsigned long)low,
                    (high != 0U) ? "CONNECTED(外部推挽驱动过它)"
                                 : "OPEN(全程无外部驱动=走线开路)");
  }

  /* 【2026-10-12】USB CDC 虚拟串口状态（迁移后的主控制台通道）。
   * 判读：
   *   cfg=0  ⟹ 主机还没完成枚举。看 rst（USB 总线复位次数）：
   *             若 rst 一直在涨，说明主机在反复重试枚举，
   *             通常意味着 D+/D- 接线、48MHz 时钟或描述符有问题。
   *   cfg=1  ⟹ 枚举成功；此后 rxBytes 应随你在 USB 串口里敲键而增长。 */
  BSP_UART_Printf("[usb] cfg=%u conn=%u rst=%lu rx=%lu tx=%lu drop=%lu\r\n",
                  (unsigned)UsbCdc_IsConfigured(), (unsigned)UsbCdc_IsConnected(),
                  (unsigned long)UsbCdc_ResetCount(),
                  (unsigned long)UsbCdc_RxBytes(),
                  (unsigned long)UsbCdc_TxBytes(),
                  (unsigned long)UsbCdc_TxDropBytes());
}

/**
  * @brief  【2026-10-11】PA10(RX) 引脚"累计跳变"监视器 —— 无观测偏差的 RX 判据
  *
  * 设计动机：
  *   BSP_UART_DiagTask 用的是"固定窗口采样"（500ms / 3s 周期）。若 PC 只发
  *   零星字节，窗口很可能整段落在两字节之间的空闲间隙里 → edges 采到 0，
  *   但这**不能**证明信号没到（这是观测偏差，本会话已踩过多次）。
  *
  *   本函数改为**累计式**：由主循环高频调用（每圈一次），维护
  *   `s_rxPinPulseCnt`（PA10 自开机以来的跳变总数）。
  *   只要 PA10 上出现过**任意一次**电平跳变，这个数就一定 > 0。
  *
  * 判读（最重要的一条判据）：
  *   s_rxPinPulseCnt > 0  ⟹ 外部信号**确实到达了 PA10 引脚硬件**，
  *                            若此时 rxIrq 仍为 0，则问题在 USART 采样环节。
  *   s_rxPinPulseCnt = 0  ⟹ 自开机以来 PA10 从未动过 ⟹ **信号未到达引脚**，
  *                            即 CH340K 的 RXD→PA10 这条外部走线/焊接有问题。
  */
void BSP_UART_RxPinPulse(void)
{
  uint32_t cur = (GPIOA->IDR & BSP_CONSOLE_RX_PIN) ? 1U : 0U;

  if (cur != s_rxPinLevel)
  {
    s_rxPinLevel = cur;
    s_rxPinPulseCnt++;
  }
}

uint32_t BSP_UART_RxPinPulseCnt(void)
{
  return s_rxPinPulseCnt;
}

/**
  * @brief  【2026-10-11】全端口引脚跳变监视器 —— 定位"CH340K 的 TXD 到底接到哪个引脚"
  *
  * 设计动机：
  *   我们已经确认 PC 侧"打开/发送"会把板子复位，导致无法在发送期间观测 MCU。
  *   但**一次 write() 只触发一次复位**（不是逐字节复位）。因此只要让 PC 做
  *   **一次超长连续发送**（几秒的量），MCU 复位重启后仍在接收数据 —— 这段
  *   重叠窗口内就一定能观测到外部信号。
  *   于是问题变成："这信号落在哪个引脚上？"
  *
  * 做法（非侵入）：
  *   `IDR` 寄存器**无论引脚配置为何种模式**都反映引脚实际电平（输入/AF/输出
  *   都读得到）。所以只要在主循环里高频读取 GPIOA..GPIOE 的 IDR，并与上次
  *   快照异或，就能`累积`出"自开机以来哪些位曾经跳变过"及其次数。
  *   **不需要改任何引脚配置**，纯粹只读，零副作用。
  *
  * 判读（关键）：
  *   本工程正常工作时**理应跳变**的位只有：
  *     PA9 (USART1_TX，打印日志)、PA13/PA14 (SWD)、
  *     PB12/PB13 (FDCAN2)、PE0/PE1 (LED)
  *   除此之外**任何**位跳变 = 外部器件在驱动它 ⟹
  *     **那就是板载 CH340K 的 TXD 实际连到的引脚！**
  *   找到后把 USART1_RX 重映射/改用对应外设即可 —— **纯软件解决**。
  *
  * 若所有端口在发送期间均无额外跳变 ⟹ CH340K TXD 确实未接到 MCU 任何引脚
  *   （走线断/未焊）⟹ 才需要改用原生 USB（PA11/PA12）方案。
  *
  * 成本：每圈 5 次 IDR 读 + 5 次异或；无跳变时约 30 周期，可忽略。
  */
void BSP_UART_PinMonitor(void)
{
  GPIO_TypeDef *ports[BSP_PINMON_PORT_NUM] = { GPIOA, GPIOB, GPIOC, GPIOD, GPIOE };
  uint32_t      p;

  if (s_pinMonInit == 0U)
  {
    for (p = 0U; p < BSP_PINMON_PORT_NUM; p++)
    {
      s_pinMonLast[p] = (uint16_t)ports[p]->IDR;
    }
    s_pinMonInit = 1U;
    return;
  }

  for (p = 0U; p < BSP_PINMON_PORT_NUM; p++)
  {
    uint16_t cur = (uint16_t)ports[p]->IDR;
    uint16_t x   = (uint16_t)(cur ^ s_pinMonLast[p]);

    if (x != 0U)
    {
      uint32_t b;

      s_pinMonEver[p] |= x;                    /* 哪些位曾经动过（无偏差） */
      for (b = 0U; b < 16U; b++)
      {
        if ((x & (uint16_t)(1U << b)) != 0U)
        {
          s_pinMonCnt[p][b]++;
        }
      }
      s_pinMonLast[p] = cur;
    }
  }
}

/**
  * @brief  把引脚跳变监视结果打到控制台（供诊断任务调用）
  */
void BSP_UART_PinMonitorDump(void)
{
  BSP_UART_Printf("[pins] ever A=%04X B=%04X C=%04X D=%04X E=%04X\r\n",
                  (unsigned)s_pinMonEver[0], (unsigned)s_pinMonEver[1],
                  (unsigned)s_pinMonEver[2], (unsigned)s_pinMonEver[3],
                  (unsigned)s_pinMonEver[4]);

  {
    GPIO_TypeDef *ports[BSP_PINMON_PORT_NUM] = { GPIOA, GPIOB, GPIOC, GPIOD, GPIOE };
    static const char kPortName[BSP_PINMON_PORT_NUM] = { 'A', 'B', 'C', 'D', 'E' };
    uint32_t p;

    for (p = 0U; p < BSP_PINMON_PORT_NUM; p++)
    {
      uint32_t b;
      char     line[160];
      int      n = 0;

      for (b = 0U; b < 16U; b++)
      {
        int room;

        if (s_pinMonCnt[p][b] == 0U)
        {
          continue;
        }
        /* 【安全】snprintf 返回"本该写入的长度"，截断时会大于实际可用空间；
         * 必须先算出剩余空间再决定是否继续，否则 sizeof(line)-n 会下溢成
         * 一个巨大的 size_t，导致越界写 → HardFault。 */
        room = (int)sizeof(line) - n;
        if (room <= 16)
        {
          break;
        }
        n += snprintf(&line[n], (size_t)room, " %c%u:%lu",
                      kPortName[p], (unsigned)b, (unsigned long)s_pinMonCnt[p][b]);
      }
      if (n > 0)
      {
        line[(n < (int)sizeof(line)) ? n : ((int)sizeof(line) - 1)] = '\0';
        BSP_UART_Printf("[pins]%s\r\n", line);
      }
    }
    (void)ports;
  }
}

/**
  * @brief  【2026-10-11】PA10 外部连通性判据 —— 内部下拉探测法（决定性、纯软件）
  *
  * 原理：
  *   串口空闲态，CH340K 的 TXD 是**推挽输出且驱动为高**（不会浮空）。
  *   而 MCU 内部上/下拉电阻约 40kΩ，远弱于对方推挽驱动。
  *   因此把 PA10 配成"输入 + 内部下拉"后读 IDR：
  *
  *     读到 **1**  ⟹ 外部有强驱动把它拉高 ⟹ **CH340K TXD 与 PA10 是连通的**，
  *                     RX 物理通路没问题（之前收不到必有别的原因）。
  *     读到 **0**  ⟹ 内部下拉把引脚拉低了 ⟹ 外部**没有任何东西在驱动它**
  *                     ⟹ CH340K TXD → PA10 这条走线**确实不通**（开路）。
  *
  * 为什么这个判据比"数边沿"更可靠：
  *   数边沿需要"采样恰好在信号跳变期间"，存在观测窗口偏差（本会话反复踩坑）；
  *   而本方法只看**静态电平**，不依赖时序，不存在窗口问题。
  *   因为 PA10 本来配的就是内部上拉（读数恒为 1，两种情况无法区分），
  *   所以必须**临时改成下拉**才能分辨 —— 这就是本测试的关键。
  *
  * @retval 1 = 外部驱动为高（连通/正常）; 0 = 被内部下拉拉低（未连通）
  */
uint8_t BSP_UART_ProbeRxPinConnected(void)
{
  GPIO_InitTypeDef gi = {0};
  uint8_t          result;

  /* 0) 先记录"当前(复用+内部上拉)"下的读数，作为对照基准 */
  s_rxProbePullUp = (GPIOA->IDR & BSP_CONSOLE_RX_PIN) ? 1U : 0U;

  /* ===== 步骤 A：只改成"输入 + 上拉" —— 电平不变（仍为高），仅换模式 =====
   * 目的：把"重配置 GPIO 本身有问题"与"电平被拉低才出问题"分开。
   * A 步若通过，说明 GPIO 重配置无害。 */
  gi.Pin  = BSP_CONSOLE_RX_PIN;
  gi.Mode = GPIO_MODE_INPUT;
  gi.Pull = GPIO_PULLUP;
  HAL_GPIO_Init(BSP_CONSOLE_RX_PORT, &gi);
  {
    uint32_t t0 = HAL_GetTick();
    while ((HAL_GetTick() - t0) < 1U) { __NOP(); }
  }
  s_rxProbeStepA = (GPIOA->IDR & BSP_CONSOLE_RX_PIN) ? 1U : 0U;   /* 预期 = 1 */
  s_rxProbeStage = 1U;                                            /* 标记：A 步已完成 */
  /* 打点：能看到这一行 ⟹ "改成输入+上拉"是安全的，问题出在后面 */
  BSP_UART_Send((s_rxProbeStepA != 0U) ? "[p]A=1\r\n" : "[p]A=0!CHECK\r\n",
                (s_rxProbeStepA != 0U) ? 8U : 14U);

  /* ===== 步骤 B：改成"输入 + 下拉" —— 把引脚拉低 =====
   * 若 A 步正常而此处复位，则说明"把 PA10 拉低"会复位整块板子 ——
   * 那将证明 PA10 并非单纯接到 CH340K 的 TXD（而是接在某个复位/控制网络上）。 */
  gi.Pull = GPIO_PULLDOWN;
  HAL_GPIO_Init(BSP_CONSOLE_RX_PORT, &gi);
  {
    uint32_t t0 = HAL_GetTick();
    while ((HAL_GetTick() - t0) < 2U) { __NOP(); }
  }
  result = (GPIOA->IDR & BSP_CONSOLE_RX_PIN) ? 1U : 0U;
  s_rxProbeStage = 2U;                                            /* 标记：B 步已完成 */
  BSP_UART_Send((result != 0U) ? "[p]B=1\r\n" : "[p]B=0\r\n",
                (result != 0U) ? 8U : 8U);

  /* 3) 立刻恢复为 USART1_RX 复用（内部上拉），不影响后续接收 */
  gi.Mode      = GPIO_MODE_AF_PP;
  gi.Pull      = GPIO_PULLUP;
  gi.Speed     = GPIO_SPEED_FREQ_HIGH;
  gi.Alternate = BSP_CONSOLE_GPIO_AF;
  HAL_GPIO_Init(BSP_CONSOLE_RX_PORT, &gi);
  s_rxProbeStage = 3U;
  BSP_UART_Send("[p]R\r\n", 6U);

  s_rxProbePullDown = result;
  return result;
}

/**
  * @brief  取上次 PA10 下拉探测结果（诊断用）
  */
uint8_t BSP_UART_RxPinProbeResult(void)
{
  return s_rxProbePullDown;
}

uint8_t BSP_UART_RxPinProbePullUp(void)
{
  return s_rxProbePullUp;
}

/**
  * @brief  【2026-10-11】边沿检测灵敏度自检 —— 证明监视器能捕获 115200 波特率信号
  *
  * 动机：
  *   监视器是"快照式"采样（比较两次调用之间的 IDR 变化）。若所有采样点都落在
  *   总线空闲期间，就永远采不到跳变 —— 实测 `[pins] A=0000`（连 PA9 都记不到），
  *   说明"没看到 PA10 跳变"这一阴性结果**尚不足以作为证据**。
  *   必须先自证检测能力。
  *
  * 做法：
  *   直接写 `USART1->TDR` 启动一个字节的发送（**立即返回**，不阻塞），
  *   然后在这 87µs（1 字节 @115200）的发送期间**狂调采样器**，
  *   PA9 正在跳变，监视器必然能记到它的边沿。
  *
  * 判读：
  *   自检后 `[pins] A` 掩码含 bit9 ⟹ 检测器对 115200 信号有效；
  *   此时若 PA10 仍无跳变，阴性结论才成立。
  */
void BSP_UART_EdgeDetectSelfTest(void)
{
  uint32_t i;

  for (i = 0U; i < 8U; i++)
  {
    /* 启动发送一个字节（0x55 跳变最多，最易被采到），立即返回 */
    USART1->TDR = 0x55U;

    /* 87µs 的发送期间高频采样（每次调用约十几条指令，可采数百次） */
    {
      uint32_t k;
      for (k = 0U; k < 200U; k++)
      {
        BSP_UART_PinMonitor();
      }
    }

    /* 等本字节真正发完，避免写 TDR 覆盖 */
    {
      uint32_t guard = 0U;
      while (((USART1->ISR & USART_ISR_TC) == 0U) && (guard < 100000U))
      {
        guard++;
      }
    }
  }

  /* 打印一个短标记，让 PA9 再动一次，然后立刻采样 */
  BSP_UART_Send("|", 1U);
  BSP_UART_PinMonitor();
  BSP_UART_PinMonitor();
}

/**
  * @brief  初始化控制台串口：时钟、GPIO复用、USART1、中断、启动接收
  */
void BSP_UART_Init(void)
{
  GPIO_InitTypeDef gpioInit = {0};

  __HAL_RCC_GPIOA_CLK_ENABLE();
  /* 【2026-10-11】引脚监视器要读 GPIOA..GPIOE 的 IDR；
   * 未开时钟的端口读 IDR 会触发总线错误，故此处统一使能。 */
  __HAL_RCC_GPIOB_CLK_ENABLE();
  __HAL_RCC_GPIOC_CLK_ENABLE();
  __HAL_RCC_GPIOD_CLK_ENABLE();
  __HAL_RCC_GPIOE_CLK_ENABLE();
  BSP_CONSOLE_CLK_ENABLE();

  /* TX推挽复用；RX上拉复用（串口空闲态为高） */
  gpioInit.Pin       = BSP_CONSOLE_TX_PIN;
  gpioInit.Mode      = GPIO_MODE_AF_PP;
  gpioInit.Pull      = GPIO_NOPULL;
  gpioInit.Speed     = GPIO_SPEED_FREQ_HIGH;
  gpioInit.Alternate = BSP_CONSOLE_GPIO_AF;
  HAL_GPIO_Init(BSP_CONSOLE_TX_PORT, &gpioInit);

  gpioInit.Pin       = BSP_CONSOLE_RX_PIN;
  gpioInit.Pull      = GPIO_PULLUP;
  HAL_GPIO_Init(BSP_CONSOLE_RX_PORT, &gpioInit);

  /* 8N1，无流控 */
  huart1.Instance          = BSP_CONSOLE_USART;
  huart1.Init.BaudRate     = BSP_CONSOLE_BAUDRATE;
  huart1.Init.WordLength   = UART_WORDLENGTH_8B;
  huart1.Init.StopBits     = UART_STOPBITS_1;
  huart1.Init.Parity       = UART_PARITY_NONE;
  huart1.Init.Mode         = UART_MODE_TX_RX;
  huart1.Init.HwFlowCtl    = UART_HWCONTROL_NONE;
  huart1.Init.OverSampling = UART_OVERSAMPLING_16;
  if (HAL_UART_Init(&huart1) != HAL_OK)
  {
    /* 【2026-10-11 加固】不再直接 Error_Handler()：
     * HAL_UART_Init 失败时它内部的 MspDeInit 已经把 USART1 时钟关掉了，
     * 进 Error_Handler 只会立刻卡死（历史上正是这条路径造成无限重启）。
     * 这里改为**把失败状态直接写死在 USART1 寄存器上**（重新使能时钟后
     * 用 CR1 打标记），并**返回**，让上层继续跑 —— 至少 LED/主循环还在，
     * 可用 SWD 或 LED 观察。 */
    __HAL_RCC_USART1_CLK_ENABLE();     /* 重新开时钟，保证寄存器可写 */
    USART1->CR1 = 0x0000DEADUL;        /* 打标记：低 16 位 0xDEAD 表示 UART 初始化失败 */
    USART1->BRR = 0xBAD00000UL;        /* 再打一个可识别的标记 */
    return;                            /* 软失败：不让整个系统卡死 */
  }

  /* 优先级低于FDCAN中断（1），串口交互实时性要求低 */
  HAL_NVIC_SetPriority(BSP_CONSOLE_USART_IRQn, 3, 0);
  HAL_NVIC_EnableIRQ(BSP_CONSOLE_USART_IRQn);

  (void)HAL_UART_Receive_IT(&huart1, &s_rxBuf, 1U);   /* 挂起首次单字节接收 */

  /* 【2026-10-11】初始化 PA10 累计跳变监视器的基准电平，
   * 避免第一次 BSP_UART_RxPinPulse() 因"0 初值 vs 实际高电平"产生假计数。 */
  s_rxPinLevel    = (GPIOA->IDR & BSP_CONSOLE_RX_PIN) ? 1U : 0U;
  s_rxPinPulseCnt = 0U;
}

/**
  * @brief  阻塞发送一段数据（超时保护100ms）
  */
void BSP_UART_Send(const char *data, uint16_t len)
{
  if ((data != NULL) && (len != 0U))
  {
    (void)HAL_UART_Transmit(&huart1, (const uint8_t *)data, len, 100U);

#if (BSP_UART_LCD_MIRROR != 0)
    /* 【2026-10-12】镜像到 TFT 滚动日志（默认关闭，见文件头说明） */
    Bsp_LcdLog_Feed(data, len);
#else
    (void)data;
#endif

    /* 【2026-10-12】USB CDC 通道：代码已就绪，等 ISR 问题解决后一行即可启用。
     * 当前 UsbCdc_Init() 未被调用（经典内核 ISR 故障），此时往发送环里堆数据
     * 没人取会一路涨到"缓冲满"，所以先判一下是否已枚举，未枚举直接跳过。 */
    if (UsbCdc_IsConfigured() != 0U)
    {
      (void)UsbCdc_Write((const uint8_t *)data, len);
    }
  }
}

/**
  * @brief  格式化打印（行缓冲96字节，够单行报文/统计输出）
  */
void BSP_UART_Printf(const char *fmt, ...)
{
  static char txBuf[96];                              /* 仅主循环调用，无重入 */
  va_list     args;
  int         n;

  va_start(args, fmt);
  n = vsnprintf(txBuf, sizeof(txBuf), fmt, args);
  va_end(args);

  if (n > 0)
  {
    if (n > (int)sizeof(txBuf))
    {
      n = (int)sizeof(txBuf);                         /* 截断保护 */
    }
    BSP_UART_Send(txBuf, (uint16_t)n);
  }
}

/**
  * @brief  当前可读字节数
  */
uint16_t BSP_UART_Available(void)
{
  uint16_t head = s_rxHead;
  uint16_t tail = s_rxTail;

  /* 【2026-10-12】把 USB CDC 的可用字节一并算上 —— 现在控制台的主输入
   * 来自 USB CDC（板载 CH340K 的 RX 走线已确认开路）。 */
  return (uint16_t)(((head - tail + BSP_CONSOLE_RX_RING_SIZE) % BSP_CONSOLE_RX_RING_SIZE)
                    + UsbCdc_Available());
}

/**
  * @brief  取走一个字节
  * @retval 字节值；-1表示缓冲为空
  * @note   【2026-10-12】优先从 USB CDC 取；USART1 那路保留兼容
  *         （若哪天 RX 走线修复/改用外接 USB-TTL，仍可直接工作）。
  */
int16_t BSP_UART_GetChar(void)
{
  uint16_t head;
  int16_t  c;

  c = UsbCdc_GetChar();
  if (c >= 0)
  {
    return c;
  }

  head = s_rxHead;

  if (s_rxTail == head)
  {
    return -1;
  }

  {
    uint8_t b = s_rxRing[s_rxTail];
    s_rxTail  = (uint16_t)((s_rxTail + 1U) % BSP_CONSOLE_RX_RING_SIZE);
    return (int16_t)b;
  }
}

/**
  * @brief  UART接收完成回调（中断上下文）：写入环形缓冲并重新挂接收
  */
void HAL_UART_RxCpltCallback(UART_HandleTypeDef *huart)
{
  if (huart->Instance == BSP_CONSOLE_USART)
  {
    uint16_t next = (uint16_t)((s_rxHead + 1U) % BSP_CONSOLE_RX_RING_SIZE);

    if (next != s_rxTail)
    {
      s_rxRing[s_rxHead] = s_rxBuf;
      s_rxHead           = next;
    }
    else
    {
      s_rxDropCnt++;                                 /* 缓冲满，丢弃 */
    }
    /* 缓冲满则丢弃本字节（控制台场景可接受，键入方会看到无回显） */
    s_rxIrqCnt++;

    (void)HAL_UART_Receive_IT(huart, &s_rxBuf, 1U);   /* 继续接收 */
  }
}
