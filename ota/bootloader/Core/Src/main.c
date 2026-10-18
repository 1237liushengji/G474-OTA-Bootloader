/**
  ******************************************************************************
  * @file    main.c
  * @brief   Bootloader 入口与启动状态机
  *
  * 启动流程（task-2 B 节）：
  *   0) 第一句显式复位向量表 SCB->VTOR = OTA_FLASH_BASE（从 APP 软复位回来时 VTOR 残留）
  *   1) HAL_Init -> SystemClock_Config -> BL_UartInit -> BL_FlashInit -> BL_ParamLoad
  *   2) 读配置 cfg（4 个候选取 seq 大且 magic/CRC 有效者；全无效 -> IDLE_A/trialLeft=0）
  *   3) activeSlot：IDLE_A/TRIAL_A -> 0；IDLE_B/TRIAL_B -> 1
  *   4) 试运行计数：TRIAL_x 先 trialLeft-- 并**立即落盘**（掉电也能累加）；减后为 0 说明
  *      新固件起不来 -> 另一槽镜像有效则切槽 + state=IDLE_<另一槽> + lastError=OTA_ERR_TRANSFER，
  *      否则进 recovery
  *   5) 启动窗口：CAN + ISO-TP 上线，最多 OTA_BOOT_WINDOW_MS 等一个完整诊断请求；
  *      收到 -> recovery；超时 -> 进入跳转判定（TRIAL 与非 TRIAL 都不额外等待）
  *   6) 跳转前 BL_ImageValidate（头 + 净固件 CRC + 独立 SP/复位向量检查）；
  *      失败试另一槽，两槽都失败 -> recovery
  *   7) recovery：CAN 常在线，主循环 轮询收帧 -> ISO-TP -> UDS 分发 -> ISO-TP 节拍/超时，
  *      **绝不主动跳转 APP**（直到上位机 31 01 FF02 激活 + 11 01 复位）
  *   8) 每圈喂狗（IWDG：LSI/32=1kHz、Reload=3000 = 3s）+ 调试 halt 冻结计数
  *
  * 说明：BL 是自包含工程，不引入既有工程的 stm32g4xx_it.c（会重名冲突），
  *       仅在本文件提供 SysTick_Handler（HAL_GetTick 的时基，必需）；
  *       其余异常/中断向量使用启动文件的默认弱定义。
  ******************************************************************************
  */
#include "bl_main.h"
#include "bl_clock.h"
#include "bl_uart.h"
#include "bl_flash_if.h"
#include "bl_crc32.h"
#include "bl_param.h"
#include "bl_image.h"
#include "bl_jump.h"
#include "bl_can.h"
#include "bl_isotp.h"
#include "bl_diag.h"

/*--------------------------------------- 模块内部变量 ------------------------------------*/
static IWDG_HandleTypeDef s_hiwdg;        /* 独立看门狗：LSI/32=1kHz，3s */
static OTA_Cfg            s_cfg;          /* 当前配置（RAM 镜像） */
static uint8_t            s_activeSlot;   /* 活动槽：0=A，1=B */
static bool               s_recovery;     /* 是否进入 recovery（永不跳转 APP） */
static bool               s_canOk;        /* CAN 是否已上线 */
static uint32_t           s_canRetryTick; /* recovery 中 CAN 重试计时 */
static bool               s_inBootWindow; /* 是否处于启动等待窗口 */

/**
  * @brief  诊断接收分发（在 BL_DiagDispatch 前加一层启动窗口过滤）
  *
  * ★★【2026-10-18 根因修复】启动窗口原先的判据是"收到任一完整诊断请求即 recovery"，
  *   而 TesterPresent(0x3E) 是**任何上位机都会周期发送的保活帧**（本项目板A 每 2s 一次）。
  *   于是对端一旦复位，0x3E 几乎必然落进 1s 窗口 → BL 置 recovery 且**永不跳 APP**
  *   → 对端永远停在 Bootloader，由 bl_diag.c 应答（这正是长期观察到的 7F 34 31 来源），
  *   OTA 永远无法开始。表现为"对端明明有镜像却一直停在 BL"。
  *
  *   正确语义：只有**表达了刷写意图**的请求才把 BL 留下来。
  *   TesterPresent 属于链路保活，必须静默忽略（既不回应，也不触发 recovery）。
  */
static void BL_RxDispatch(const uint8_t *data, uint16_t len)
{
  if (s_inBootWindow && (len >= 1U))
  {
    if (data[0] == 0x3EU)                       /* TesterPresent：保活帧 */
    {
      return;
    }
    s_recovery = true;                          /* 其余请求 → 留在 BL */
  }

  BL_DiagDispatch(data, len);
}

/*--------------------------------------- 基础服务 ----------------------------------------*/
/**
  * @brief  SysTick 中断服务函数（HAL 时基）
  * @note   没有它 HAL_GetTick/HAL_Delay 永远不走，启动窗口也会死等
  */
void SysTick_Handler(void)
{
  HAL_IncTick();
}

/**
  * @brief  初始化独立看门狗（与既有工程 main.c 完全一致的取值）
  */
static void BL_IwdgInit(void)
{
  DBGMCU->APB1FZR1 |= DBGMCU_APB1FZR1_DBG_IWDG_STOP;  /* 调试器 halt 时冻结计数 */

  s_hiwdg.Instance       = IWDG;
  s_hiwdg.Init.Prescaler = IWDG_PRESCALER_32;         /* LSI 32kHz / 32 = 1kHz */
  s_hiwdg.Init.Reload    = 3000U;                     /* 3s 超时 */
  s_hiwdg.Init.Window    = IWDG_WINDOW_DISABLE;
  if (HAL_IWDG_Init(&s_hiwdg) != HAL_OK)
  {
    Error_Handler();
  }
}

/**
  * @brief  打印当前配置（现场诊断）
  */
static void BL_LogCfg(const char *tag)
{
  BL_UartPrintf("%s state=%lu trialLeft=%lu activeSlot=%u lastErr=0x%08lX\r\n",
                tag,
                (unsigned long)s_cfg.state,
                (unsigned long)s_cfg.trialLeft,
                (unsigned)s_activeSlot,
                (unsigned long)s_cfg.lastError);
}

/*--------------------------------------- 状态机 ------------------------------------------*/
/**
  * @brief  处理试运行计数（必须在跳转判定之前完成递减并落盘）
  */
static void BL_HandleTrial(void)
{
  uint8_t      other;
  BL_ImageInfo info;

  if ((s_cfg.state != OTA_ST_TRIAL_A) && (s_cfg.state != OTA_ST_TRIAL_B))
  {
    return;                                     /* 非试运行状态：不动计数器 */
  }

  /* [TRIAL-DEC] 先递减并立即写回：即使随后掉电，计数也已生效，不会无限重试。
   * 配合 OTA_TRIAL_BOOT_LIMIT=3，实际时序是"激活写入 left=3 -> 第1次启动减为2(可运行)
   * -> 第2次减为1(可运行) -> 第3次减为0(触发回滚)"。
   * 注意：因为递减发生在**本次**启动的跳转判定之前（见 main() step4 在 step6 之前），
   * left=3 并不等于"能运行 3 次"，新固件实际只有 2 次运行机会。
   * 若日后要让新固件跑满 3 次，需把 OTA_TRIAL_BOOT_LIMIT 提到 4，或把递减移到跳转之后。 */
  if (s_cfg.trialLeft > 0U)
  {
    s_cfg.trialLeft--;
  }
  if (!BL_ParamStore(&s_cfg))
  {
    BL_UART_SEND_STR("BL: trial counter store FAIL\r\n");
  }

  if (s_cfg.trialLeft > 0U)
  {
    return;                                     /* 还有试运行机会：继续启动该槽 */
  }

  /* [TRIAL-ZERO] 试运行次数用尽：判定新固件起不来 -> 回滚到另一槽（若其镜像完整有效） */
  other = (uint8_t)(1U - s_activeSlot);
  if (BL_ImageValidate(other, &info) == BL_IMG_OK)
  {
    s_activeSlot    = other;
    s_cfg.state     = (other == 0U) ? OTA_ST_IDLE_A : OTA_ST_IDLE_B;
    s_cfg.trialLeft = 0U;
    s_cfg.lastError = OTA_ERR_TRANSFER;
    if (!BL_ParamStore(&s_cfg))
    {
      BL_UART_SEND_STR("BL: rollback store FAIL\r\n");
    }
    BL_UartPrintf("BL: trial exhausted -> rollback to slot %u\r\n", (unsigned)other);
  }
  else
  {
    s_recovery = true;
    BL_UART_SEND_STR("BL: trial exhausted, no valid image -> recovery\r\n");
  }
}

/**
  * @brief  启动窗口：OTA_BOOT_WINDOW_MS 内等待一个"表达刷写意图"的诊断请求
  * @note   TesterPresent(0x3E) 不在此列 —— 它只是保活帧，见 BL_RxDispatch。
  */
static void BL_BootWindow(void)
{
  uint32_t start;

  if (!s_canOk)
  {
    BL_UART_SEND_STR("BL: CAN offline, skip boot window\r\n");
    return;
  }

  start  = HAL_GetTick();
  s_inBootWindow = true;                        /* ★ 窗口内由 BL_RxDispatch 过滤 0x3E */

  while ((HAL_GetTick() - start) < OTA_BOOT_WINDOW_MS)
  {
    BL_CanFrame frame;

    while (BL_CanPoll(&frame))
    {
      BL_IsoTpOnCanFrame(frame.id, frame.data, frame.len);
    }
    BL_IsoTpTask();
    HAL_IWDG_Refresh(&s_hiwdg);

    /* ★ 只信 s_recovery（由 BL_RxDispatch 判定"是否表达刷写意图"）。
     *   不能再用 BL_IsoTpGetRxMsgCount()，它在本层已按"收到完整消息"自增，
     *   TesterPresent 一样会让它 +1，等于把过滤完全绕过。 */
    if (s_recovery || BL_IsoTpRxBusy())
    {
      s_recovery = true;
      BL_UART_SEND_STR("BL: diag request in boot window -> recovery\r\n");
      s_inBootWindow = false;
      return;
    }
  }

  s_inBootWindow = false;

  /* 窗口超时：非 TRIAL 与 TRIAL 都直接进入跳转判定（TRIAL 不额外等待） */
}

/**
  * @brief  跳转判定：校验活动槽 -> 失败试另一槽 -> 都失败进 recovery
  */
static void BL_TryBootApp(void)
{
  BL_ImageInfo info;
  BL_ImgResult result;
  uint8_t      attempt;
  uint8_t      slot;

  for (attempt = 0U; attempt < 2U; attempt++)
  {
    slot = (attempt == 0U) ? s_activeSlot : (uint8_t)(1U - s_activeSlot);

    /* [JUMP-CHECK] 跳转前完整校验：镜像头 + 净固件 CRC32 + **独立的** 栈顶指针/复位向量检查 */
    result = BL_ImageValidate(slot, &info);

    if (result == BL_IMG_OK)
    {
      BL_UartPrintf("BL: jump to APP slot %u base=0x%08lX size=0x%lX\r\n",
                    (unsigned)slot,
                    (unsigned long)info.appBase,
                    (unsigned long)info.payloadSize);
      HAL_IWDG_Refresh(&s_hiwdg);               /* 跳转前喂狗，给 APP 初始化留最长时间 */
      BL_JumpToApp(info.appBase);               /* 不返回 */
    }

    /* 校验失败：记录 lastError 便于现场诊断，然后尝试另一槽 */
    s_cfg.lastError = BL_ImageErrToOtaErr(result);
    if (!BL_ParamStore(&s_cfg))
    {
      BL_UART_SEND_STR("BL: err record store FAIL\r\n");
    }
    BL_UartPrintf("BL: slot %u image invalid (err=0x%08lX)\r\n",
                  (unsigned)slot,
                  (unsigned long)s_cfg.lastError);
  }

  s_recovery = true;
  BL_UART_SEND_STR("BL: no valid image -> recovery\r\n");
}

/*--------------------------------------- 入口 --------------------------------------------*/
/**
  * @brief  The application entry point.
  * @retval int（永不返回）
  */

/* ==================== 分步自检指示（诊断用） ====================
 * 目的：上电"没反应"时无法判断卡在哪。LED 闪 N 次 = 走到第 N 步。
 * LED: PE0，低电平点亮（与 APP 的 led.h 一致）。
 * 用直接寄存器访问，不引入 HAL_GPIO 依赖，保持 BL 体积。
 * 若不需要，把 BL_DIAG_LED 置 0 即可（整个诊断块会被编译掉）。
 * ============================================================== */
#define BL_BARE_BLINK 0    /* 置 1 = 只做裸机闪灯自检，便于隔离环境问题 */
/* 置 1 = 上电把当前配置**落盘一次**（走 OTA 落定/回滚用的同一条写路径）。
 * 用途：在没有可用串口（被串口助手占用），或"调试器一连接就把内核 halt/复位、
 *       导致读寄存器只能读到复位值"的条件下，仍能判定两件事：
 *         ① 固件确实执行到了 main()（配置被写进 Flash，是掉电/复位都不消失的证据）；
 *         ② 写控制区（整页擦除页 0）**不会**擦掉 BL 自己（这是 2026-10-05 变砖事故的
 *            正向回归验证：那时页 0 里塞着代码，一写就自毁）。
 * 判据：复位运行后读 0x080001D8 应出现 magic="G4BL"、seq=1 的有效副本。 */
#define BL_BOOT_BREADCRUMB 0
#define BL_DIAG_LED 1
#define BL_LED_PORT          GPIOE
#define BL_LED_PIN           0U

/* 每毫秒的空循环次数（约 1ms @150MHz，仅用于诊断指示） */
#define BL_DIAG_UNITS_PER_MS 8000U

static void BL_DiagLedInit(void)
{
  RCC->AHB2ENR |= RCC_AHB2ENR_GPIOEEN;
  (void)RCC->AHB2ENR;
  BL_LED_PORT->MODER = (BL_LED_PORT->MODER & ~(3UL << (BL_LED_PIN * 2U)))
                     | (1UL << (BL_LED_PIN * 2U));
  BL_LED_PORT->BSRR = (1UL << BL_LED_PIN);          /* 先熄灭（高=灭） */
}

static void BL_DiagLedOn(void)  { BL_LED_PORT->BRR  = (1UL << BL_LED_PIN); }
static void BL_DiagLedOff(void) { BL_LED_PORT->BSRR = (1UL << BL_LED_PIN); }

/* 粗略延时（诊断用，精度不重要）。
 * ⚠️ 不要写成 `for (volatile uint32_t i = 0; i < 24000U; i++)`：
 *    volatile 计数器 + 编译期常量上界会让 ARMCLANG **完全展开**该循环，
 *    实测使 main.o 从 1KB 膨胀到 24KB，直接把 BL 顶出分区。
 *    这里上界由参数传入（运行期值）+ noinline，编译器不会展开。 */
__attribute__((noinline))
static void BL_DiagDelay(uint32_t units)
{
  while (units-- != 0U)
  {
    uint32_t i = BL_DIAG_UNITS_PER_MS;
    while (i-- != 0U)
    {
      __NOP();
    }
  }
}

/* 闪 stage 次；offMs 为该步之后额外的熄灭时长（用于区分同类步骤） */
__attribute__((noinline))
static void BL_DiagStage(uint32_t stage, uint32_t offMs)
{
  for (uint32_t i = 0U; i < stage; i++)
  {
    BL_DiagLedOn();
    BL_DiagDelay(60U);
    BL_DiagLedOff();
    BL_DiagDelay(90U);
  }
  BL_DiagDelay(offMs);
}

/* ============ 裸机闪灯自检（BL_BARE_BLINK=1 时生效） ============
 * 上电后 LED(PE0) 约 2Hz 闪烁，用于判定"MCU 到底有没有在执行代码"。
 * 刻意不调用 HAL / 不配置时钟 / 不初始化外设，把变量降到最低。
 * ============================================================== */
#if (BL_BARE_BLINK != 0)

/* ============ 裸机诊断固件 v3 ============
 * 目标：在"什么都不确定"的情况下，用 LED 与串口回答三个问题——
 *   1. MCU 是否在执行代码            -> LED 心跳
 *   2. 时钟/延时是否正常             -> 心跳周期是否为 3 秒
 *   3. USART1 能否发出数据           -> 串口是否有输出 + LED 尾标
 * 全寄存器操作，不调用 HAL；不依赖 HSE（HSI 16MHz 直接作系统时钟）。
 * ======================================== */

static void Bare_LedInit(void)
{
  RCC->AHB2ENR |= (1UL << 4);                 /* GPIOEEN */
  (void)RCC->AHB2ENR;
  GPIOE->MODER   = (GPIOE->MODER & ~(3UL << 0)) | (1UL << 0);
  GPIOE->OTYPER &= ~(1UL << 0);
  GPIOE->OSPEEDR = (GPIOE->OSPEEDR & ~(3UL << 0)) | (3UL << 0);
  GPIOE->PUPDR   = (GPIOE->PUPDR & ~(3UL << 0));
}

static void Bare_LedOn(void)  { GPIOE->BRR  = (1UL << 0); }
static void Bare_LedOff(void) { GPIOE->BSRR = (1UL << 0); }

/* 延时 ms。
 * ⚠️ 计数器必须 volatile：否则 `while (i--) { __NOP(); }` 无副作用，
 *    编译器会把它整体删除，延时瞬间返回（表现为 LED 爆闪）。
 * ⚠️ 上界必须是运行期值（这里来自参数 ms）：若为编译期常量，会触发完全展开。 */
__attribute__((noinline))
static void Bare_DelayMs(uint32_t ms)
{
  while (ms-- != 0U)
  {
    volatile uint32_t i = 4000U;              /* HSI 16MHz 下约 1ms */
    while (i != 0U)
    {
      i--;
    }
  }
}

static void Bare_UartInit(void)
{
  /* USART1 内核时钟明确选 HSI16（= SYSCLK，避免 SystemInit 未配时钟的歧义） */
  RCC->CCIPR = (RCC->CCIPR & ~(3UL << 0)) | (2UL << 0);   /* USART1SEL = HSI16 */

  RCC->AHB2ENR |= (1UL << 0);                 /* GPIOAEN */
  RCC->APB2ENR |= (1UL << 14);                /* USART1EN */
  (void)RCC->APB2ENR;

  /* PA9 = USART1_TX, PA10 = USART1_RX，AF7，推挽，高速 */
  GPIOA->MODER   = (GPIOA->MODER & ~((3UL << 18) | (3UL << 20)))
                 | (2UL << 18) | (2UL << 20);
  GPIOA->OSPEEDR = (GPIOA->OSPEEDR & ~((3UL << 18) | (3UL << 20)))
                 | (3UL << 18) | (3UL << 20);
  GPIOA->PUPDR   = (GPIOA->PUPDR & ~((3UL << 18) | (3UL << 20)))
                 | (1UL << 20);               /* RX 上拉 */
  GPIOA->AFR[1]  = (GPIOA->AFR[1] & ~((0xFUL << 4) | (0xFUL << 8)))
                 | (7UL << 4) | (7UL << 8);

  USART1->CR1 = 0U;
  USART1->CR2 = 0U;
  USART1->CR3 = 0U;
  /* HSI16, OVER8=0 -> USARTDIV = 16e6/(16*115200) = 8.6806
   * BRR = (8 << 4) | round(0.6806*16) = 0x8B */
  USART1->BRR = 0x8BU;
  USART1->CR1 = (1UL << 3) | (1UL << 2) | (1UL << 0);   /* TE | RE | UE */
}

/* 发送字符串；返回 1 = 全部发出，0 = 超时（TXE 未置位）。
 * ⚠️ 绝不无限等待：否则串口有问题时整个程序会卡死，LED 也停。 */
static uint32_t Bare_Send(const char *s)
{
  while (*s != '\0')
  {
    uint32_t guard = 2000000U;
    while (((USART1->ISR & (1UL << 7)) == 0U) && (guard != 0U))
    {
      guard--;
    }
    if (guard == 0U)
    {
      return 0U;                              /* 超时：放弃本次发送 */
    }
    USART1->TDR = (uint32_t)(uint8_t)(*s);
    s++;
  }
  return 1U;
}

int main(void)
{
  static const char *const msgs[3] = {
    "BARE3: MCU alive, HSI 16MHz, no HSE used\r\n",
    "BARE3: USART1 115200 8N1 on PA9\r\n",
    "BARE3: led heartbeat = flash N times, 1s gap\r\n"
  };
  uint32_t round = 0U;
  uint32_t i;

  /* 首句（第一条**可执行**语句）显式复位向量表，与正常 BL 的 main 一致。
   * ⚠️ 不可省：静态守卫要求"main() 第一条语句是 SCB->VTOR = OTA_FLASH_BASE"，
   *    用于防止从 APP 软复位回来时 VTOR 仍指向 APP 的向量表。 */
  SCB->VTOR = OTA_FLASH_BASE;

  Bare_LedInit();
  Bare_UartInit();

  Bare_LedInit();
  Bare_UartInit();
  Bare_LedOff();

  for (;;)
  {
    round++;
    if (round > 3U)
    {
      round = 1U;
    }

    /* 闪 round 次（每次 120ms 亮 / 130ms 灭），随后停 1 秒 */
    for (i = 0U; i < round; i++)
    {
      Bare_LedOn();
      Bare_DelayMs(120U);
      Bare_LedOff();
      Bare_DelayMs(130U);
    }
    Bare_DelayMs(1000U);

    /* 每轮发一条消息；成功则该轮结束后长亮 300ms 作为"串口 OK"尾标 */
    if (Bare_Send(msgs[round - 1U]) != 0U)
    {
      Bare_LedOn();
      Bare_DelayMs(300U);
      Bare_LedOff();
    }
  }
}

#else  /* ---------------- 正常 Bootloader ---------------- */

int main(void)
{
  /* 第一句：显式复位向量表。从 APP 分支软复位回来时 VTOR 仍指向 APP，
   * 必须在 HAL_Init() 之前拉回 BL 自己的向量表 */
  SCB->VTOR = OTA_FLASH_BASE;

  /* ---- 分步自检指示：能亮第 1 下，说明 MCU 在跑、Flash 可执行 ---- */
  BL_DiagLedInit();
  BL_DiagStage(1U, 0U);

  /* 1) 基础初始化：时钟/日志/Flash/配置 */
  HAL_Init();
  SystemClock_Config();                           /* HSE 8M -> PLL -> 150MHz */
  /* 能到第 2 步 => HSE 起振且成功切到 150MHz（HSE 坏会停在 Error_Handler 死循环） */
  BL_DiagStage(2U, 0U);

  BL_UartInit();
  BL_DiagStage(3U, 0U);

  /* 上报实际时钟源：区分"晶振正常"与"已回落 HSI"。
   * 这条日志是定位"上电没反应"的关键——若打印 HSE failed，说明板上 HSE 未起振。 */
  if (BL_ClockSource() == BL_CLK_HSI_FALLBACK)
  {
    BL_UART_SEND_STR("BL: !! HSE FAILED -> running on HSI+PLL 150MHz\r\n");
  }
  else if (BL_ClockSource() == BL_CLK_HSE)
  {
    BL_UART_SEND_STR("BL: clock HSE+PLL 150MHz OK\r\n");
  }
  else
  {
    BL_UART_SEND_STR("BL: clock source unknown\r\n");
  }

  /* CRC32 自检必须早于任何 Flash 操作：BL/APP/上位机三方一致性的最后防线 */
  if (!BL_FlashSelfTest())
  {
    BL_UART_SEND_STR("BL: crc self-test FAIL\r\n");
    BL_DiagStage(4U, 1500U);                      /* 长灭 = 自检失败 */
  }
  BL_DiagStage(4U, 0U);

  (void)BL_FlashInit();

  BL_IwdgInit();                                  /* 先起狗：后面所有擦写路径都有喂狗点 */
  BL_FlashAttachWatchdog(&s_hiwdg);

  /* 2) 读配置（4 个候选） */
  if (!BL_ParamLoad(&s_cfg))
  {
    BL_UART_SEND_STR("BL: cfg invalid, use default IDLE_A\r\n");
  }

#if (BL_BOOT_BREADCRUMB != 0)
  /* 上电自证打点：把配置落盘一次。见文件上方 BL_BOOT_BREADCRUMB 的说明。
   * 这是"无串口/调试器抢 halt"环境下唯一不受干扰的证据：Flash 写入不会被 halt 抹掉。 */
  if (!BL_ParamStore(&s_cfg))
  {
    BL_UART_SEND_STR("BL: breadcrumb store FAIL\r\n");
  }
#endif

  /* 3) 活动槽 */
  s_activeSlot = BL_ParamSlotOfState(s_cfg.state);
  BL_LogCfg("BL: boot");

  /* 4) 试运行计数（必须在跳转判定之前） */
  BL_HandleTrial();
  BL_LogCfg("BL: after trial");
  BL_DiagStage(5U, 0U);

  /* DIAG 层必须在 ISO-TP 回调可用之前初始化：它决定下载/擦除的目标槽 = 活动槽之外的另一个 */
  BL_DiagInit(s_activeSlot);

  /* 5) CAN + ISO-TP 上线，开启动窗口 */
  s_canOk = (BL_CanInit() == HAL_OK);
  if (s_canOk)
  {
    BL_IsoTpInit(BL_DIAG_REQ_ID, BL_DIAG_RESP_ID, BL_RxDispatch);
    BL_DiagStage(6U, 0U);
  }
  else
  {
    BL_UART_SEND_STR("BL: can init FAIL\r\n");
    BL_DiagStage(6U, 1500U);                      /* 长灭 = CAN 未上线 */
  }

  if (!s_recovery)
  {
    BL_BootWindow();
  }

  /* 6) 跳转判定 */
  if (!s_recovery)
  {
    BL_TryBootApp();                              /* 成功则在此跳转，不返回 */
  }

  /* 7) recovery 模式：CAN 常在线，绝不跳转 APP */
  BL_UART_SEND_STR("BL: recovery mode (wait diag)\r\n");
  s_canRetryTick = HAL_GetTick();

  for (;;)
  {
    if (s_canOk)
    {
      BL_CanFrame frame;

      while (BL_CanPoll(&frame))
      {
        BL_IsoTpOnCanFrame(frame.id, frame.data, frame.len);
      }
      BL_IsoTpTask();                             /* STmin 节拍发 CF + N_Bs/N_Cr 超时 */
      BL_DiagTask();                              /* S3 会话超时 */
    }
    else if ((HAL_GetTick() - s_canRetryTick) >= 2000U)
    {
      /* CAN 初始化失败时每 2s 重试（收发器上电慢/总线未就绪），不放弃现场恢复能力 */
      s_canRetryTick = HAL_GetTick();
      if (BL_CanInit() == HAL_OK)
      {
        s_canOk = true;
        BL_IsoTpInit(BL_DIAG_REQ_ID, BL_DIAG_RESP_ID, BL_RxDispatch);
        BL_UART_SEND_STR("BL: CAN online\r\n");
      }
    }
    else
    {
      /* 等待重试时隙 */
    }

    HAL_IWDG_Refresh(&s_hiwdg);                   /* 每圈喂狗：主循环健康证明 */
  }
}

#endif /* BL_BARE_BLINK（正常 Bootloader 部分到此结束） */

/**
  * @brief  初始化致命错误处理
  * @note   关中断后死循环：IWDG（已使能）会在 3s 后复位，形成可控的重试，
  *         而不是静默挂死。仅用于启动期不可恢复的初始化失败（时钟/串口/看门狗）。
  * @note   刻意放在 BL_BARE_BLINK 条件**之外**：bl_clock.c 等模块始终引用它，
  *         否则裸机闪灯版会报 Undefined symbol Error_Handler。
  */
void Error_Handler(void)
{
  __disable_irq();
  for (;;)
  {
  }
}

#ifdef USE_FULL_ASSERT
/**
  * @brief  HAL 断言失败上报
  */
void assert_failed(uint8_t *file, uint32_t line)
{
  (void)file;
  (void)line;
}
#endif /* USE_FULL_ASSERT */
