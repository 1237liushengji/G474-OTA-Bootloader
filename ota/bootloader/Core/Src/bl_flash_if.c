/**
  ******************************************************************************
  * @file    bl_flash_if.c
  * @brief   Bootloader Flash 接口实现：擦除 / 双字编程 / 校验 / 回读 + 写保护
  *
  * 硬件事实（STM32G474VET6，见 ota_common.h）：
  *   - 页大小 2KB；编程最小粒度 64 位（双字）：
  *     HAL_FLASH_Program(FLASH_TYPEPROGRAM_DOUBLEWORD, addr, u64)
  *   - Flash 可能被选项字节配成双 Bank（DBANK=1），此时**页号是 Bank 内偏移**、
  *     且必须显式选择 Bank；单 Bank（DBANK=0）时页号 = 绝对偏移 / 2KB。
  *     本文件按 FLASH->OPTR 的 DBANK 位判定后换算（见 BL_FlashPageOf）。
  *   - 擦除一页耗时 20~40ms，IWDG 超时 3s：**每页擦除/每轮编程前后都必须喂狗**。
  *
  * 保护不变量（硬性，对应 task-2 C 节）：
  *   公开接口 BL_FlashEraseRange / BL_FlashWrite 对任何 addr < OTA_SLOT_A_ADDR
  *   直接返回 OTA_FLASH_ERR_PROTECT —— 即 BL 向量表页(0x08000000)、控制区主份、
  *   以及 [OTA_BL_CODE_ADDR, OTA_SLOT_A_ADDR) 的 BL 代码区，诊断下载流程永远碰不到。
  *   控制区副本的写入只能经 ...Ex(..., allowCfg=true) 的受控旁路，且旁路只额外
  *   放行两个控制页（每页 2KB），其余区域判定完全一致。
  ******************************************************************************
  */
#include "bl_flash_if.h"
#include "bl_crc32.h"
#include <string.h>

/*--------------------------------------- 模块内部宏 --------------------------------------*/
#define BL_FLASH_DW_SIZE    8U                        /* 双字编程粒度 */
#define BL_FLASH_DW_MASK    (BL_FLASH_DW_SIZE - 1U)

/*--------------------------------------- 模块内部变量 ------------------------------------*/
static IWDG_HandleTypeDef *s_pIwdg;                   /* 由 main 在 HAL_IWDG_Init 后挂接 */

/**
  * @brief  喂狗（擦/写前后调用，避免 3s 看门狗在长擦写中复位）
  * @note   句柄未挂接时不做任何事（BL 早期初始化阶段）
  */
static void BL_FlashKickDog(void)
{
  if (s_pIwdg != NULL)
  {
    (void)HAL_IWDG_Refresh(s_pIwdg);
  }
}

/**
  * @brief  把绝对地址换算成 HAL 需要的"页号 + Bank"
  * @param  addr 绝对地址（必须页对齐）
  * @param  bank 输出：FLASH_BANK_1 / FLASH_BANK_2
  * @retval 页号（Bank 内偏移 / 2KB）
  * @note   双 Bank(DBANK=1) 时 0x08040000 起的页属于 Bank2 且页号从 0 重新计数；
  *         单 Bank(DBANK=0) 时整片线性，页号 = (addr - FLASH_BASE) / 2KB。
  *         若设备头未提供 DBANK 位定义（FLASH_OPTR_DBANK），退化为单 Bank 线性换算。
  */
static uint32_t BL_FlashPageOf(uint32_t addr, uint32_t *bank)
{
  uint32_t offset = addr - OTA_FLASH_BASE;

#if defined(FLASH_OPTR_DBANK)
  if ((FLASH->OPTR & FLASH_OPTR_DBANK) != 0U)
  {
    if (offset >= OTA_FLASH_BANK_SIZE)
    {
      *bank = FLASH_BANK_2;
      offset -= OTA_FLASH_BANK_SIZE;
    }
    else
    {
      *bank = FLASH_BANK_1;
    }
  }
  else
  {
    *bank = FLASH_BANK_1;
  }
#else
  *bank = FLASH_BANK_1;
#endif

  return offset / OTA_FLASH_PAGE_SIZE;
}

/**
  * @brief  区域白名单：只有 APP 槽（可选加控制页）允许擦写
  * @param  addr      起始地址
  * @param  size      字节数
  * @param  allowCfg  true=额外放行两个控制页（仅 bl_param.c 使用）
  * @retval true=允许
  */
static bool BL_FlashRegionAllowed(uint32_t addr, uint32_t size, bool allowCfg)
{
  uint32_t end;

  if (size == 0U)
  {
    return false;
  }
  if (addr > OTA_FLASH_END)
  {
    return false;
  }
  if (size > ((OTA_FLASH_END - addr) + 1U))          /* 越界/回绕保护 */
  {
    return false;
  }
  end = addr + size - 1U;

  if (allowCfg)
  {
    /* 控制区主份页：页首 OTA_CFG_VECTAB_BYTES 是 BL 向量表，由调用方负责原样保全 */
    if ((addr >= OTA_CFG_PRIMARY_ADDR) && (end < (OTA_CFG_PRIMARY_ADDR + OTA_CFG_PAGE_SIZE)))
    {
      return true;
    }
    if ((addr >= OTA_CFG_MIRROR_ADDR) && (end < (OTA_CFG_MIRROR_ADDR + OTA_CFG_PAGE_SIZE)))
    {
      return true;
    }
  }

  /* APP 两个槽（含槽首 32B 镜像头） */
  if ((addr >= OTA_SLOT_A_ADDR) && (end < (OTA_SLOT_A_ADDR + OTA_SLOT_SIZE)))
  {
    return true;
  }
  if ((addr >= OTA_SLOT_B_ADDR) && (end < (OTA_SLOT_B_ADDR + OTA_SLOT_SIZE)))
  {
    return true;
  }

  return false;
}

/**
  * @brief  擦除内部实现（按页循环，每页调 HAL_FLASHEx_Erase 并复查错误标志）
  * @param  addr      起始地址（必须页对齐）
  * @param  size      字节数（内部向上取整到页）
  * @param  allowCfg  是否放行控制区页
  */
static OTA_FlashResult BL_FlashEraseRangeInternal(uint32_t addr, uint32_t size, bool allowCfg)
{
  OTA_FlashResult result = OTA_FLASH_OK;
  uint32_t        pages;
  uint32_t        eraseBytes;
  uint32_t        i;

  if ((addr % OTA_FLASH_PAGE_SIZE) != 0U)
  {
    return OTA_FLASH_ERR_RANGE;                      /* 起始地址必须页对齐 */
  }
  if (size == 0U)
  {
    return OTA_FLASH_ERR_RANGE;
  }

  pages = (size + OTA_FLASH_PAGE_SIZE - 1U) / OTA_FLASH_PAGE_SIZE;   /* 向上取整到页 */
  if (pages > (OTA_FLASH_SIZE / OTA_FLASH_PAGE_SIZE))
  {
    return OTA_FLASH_ERR_RANGE;
  }
  eraseBytes = pages * OTA_FLASH_PAGE_SIZE;

  if (!BL_FlashRegionAllowed(addr, eraseBytes, allowCfg))
  {
    return OTA_FLASH_ERR_PROTECT;
  }

  if (HAL_FLASH_Unlock() != HAL_OK)
  {
    return OTA_FLASH_ERR_LOCK;
  }

  for (i = 0U; i < pages; i++)
  {
    FLASH_EraseInitTypeDef eraseInit;
    uint32_t               pageError = 0xFFFFFFFFU;
    uint32_t               bank      = FLASH_BANK_1;
    uint32_t               pageIndex;

    BL_FlashKickDog();                               /* [IWDG-PRE-ERASE] 擦除前喂狗 */

    pageIndex = BL_FlashPageOf(addr + (i * OTA_FLASH_PAGE_SIZE), &bank);

    eraseInit.TypeErase = FLASH_TYPEERASE_PAGES;
    eraseInit.Banks     = bank;                      /* 双 Bank 时必须显式选 Bank */
    eraseInit.Page      = pageIndex;                 /* Bank 内页号 */
    eraseInit.NbPages   = 1U;                        /* 每页单独擦：便于逐页复查与喂狗 */

    if (HAL_FLASHEx_Erase(&eraseInit, &pageError) != HAL_OK)
    {
      result = OTA_FLASH_ERR_ERASE;
      break;
    }
    if (FLASH_WaitForLastOperation(FLASH_TIMEOUT_VALUE) != HAL_OK)
    {
      result = OTA_FLASH_ERR_ERASE;
      break;
    }
    if (__HAL_FLASH_GET_FLAG(FLASH_FLAG_ALL_ERRORS) != 0U)
    {
      __HAL_FLASH_CLEAR_FLAG(FLASH_FLAG_ALL_ERRORS);
      result = OTA_FLASH_ERR_ERASE;
      break;
    }
    __HAL_FLASH_CLEAR_FLAG(FLASH_FLAG_ALL_ERRORS);

    BL_FlashKickDog();                               /* [IWDG-POST-ERASE] 擦除后喂狗 */
  }

  (void)HAL_FLASH_Lock();
  return result;
}

/**
  * @brief  编程内部实现（小端拼 64 位双字，长度向上补齐到 8 的倍数、尾部填 0xFF）
  */
static OTA_FlashResult BL_FlashWriteInternal(uint32_t addr, const uint8_t *data, uint32_t len, bool allowCfg)
{
  OTA_FlashResult result = OTA_FLASH_OK;
  uint32_t        padded;
  uint32_t        i;

  if ((data == NULL) || (len == 0U))
  {
    return OTA_FLASH_ERR_RANGE;
  }
  if ((addr % BL_FLASH_DW_SIZE) != 0U)
  {
    return OTA_FLASH_ERR_RANGE;                      /* 必须 8 字节对齐 */
  }

  padded = (len + BL_FLASH_DW_MASK) & ~BL_FLASH_DW_MASK;   /* 向上补齐到 8 的倍数 */
  if (padded < len)
  {
    return OTA_FLASH_ERR_RANGE;                      /* 长度回绕保护 */
  }
  if (!BL_FlashRegionAllowed(addr, padded, allowCfg))
  {
    return OTA_FLASH_ERR_PROTECT;
  }

  if (HAL_FLASH_Unlock() != HAL_OK)
  {
    return OTA_FLASH_ERR_LOCK;
  }

  BL_FlashKickDog();                                 /* [IWDG-PRE-WRITE] 编程前喂狗 */

  for (i = 0U; i < padded; i += BL_FLASH_DW_SIZE)
  {
    uint64_t dw = 0ULL;
    uint32_t j;

    for (j = 0U; j < BL_FLASH_DW_SIZE; j++)
    {
      uint8_t b = 0xFFU;                             /* 补齐字节固定 0xFF：不改变未写字节 */

      if ((i + j) < len)
      {
        b = data[i + j];
      }
      dw |= ((uint64_t)b << (8U * j));               /* 小端：低字节对应低地址 */
    }

    if (HAL_FLASH_Program(FLASH_TYPEPROGRAM_DOUBLEWORD, addr + i, dw) != HAL_OK)
    {
      result = OTA_FLASH_ERR_PROGRAM;
      break;
    }
    if (FLASH_WaitForLastOperation(FLASH_TIMEOUT_VALUE) != HAL_OK)
    {
      result = OTA_FLASH_ERR_PROGRAM;
      break;
    }
    if (__HAL_FLASH_GET_FLAG(FLASH_FLAG_ALL_ERRORS) != 0U)
    {
      __HAL_FLASH_CLEAR_FLAG(FLASH_FLAG_ALL_ERRORS);
      result = OTA_FLASH_ERR_PROGRAM;
      break;
    }
    __HAL_FLASH_CLEAR_FLAG(FLASH_FLAG_ALL_ERRORS);
  }

  BL_FlashKickDog();                                 /* [IWDG-POST-WRITE] 编程后喂狗 */

  (void)HAL_FLASH_Lock();
  return result;
}

/**
  * @brief  初始化：清 Flash 错误标志（上电可能残留 EOP/OPERR 等）
  */
bool BL_FlashInit(void)
{
  __HAL_FLASH_CLEAR_FLAG(FLASH_FLAG_ALL_ERRORS);
  return true;
}

/**
  * @brief  挂接 IWDG 句柄（main 在 HAL_IWDG_Init 之后调用）
  */
void BL_FlashAttachWatchdog(IWDG_HandleTypeDef *hiwdg)
{
  s_pIwdg = hiwdg;
}

/**
  * @brief  擦除一段 Flash（公开接口，绝对保护 BL 自身与控制区）
  * @param  addr 起始地址（必须页对齐）
  * @param  size 字节数（内部向上取整到页）
  */
OTA_FlashResult BL_FlashEraseRange(uint32_t addr, uint32_t size)
{
  if (addr < OTA_SLOT_A_ADDR)
  {
    /* 保护不变量：BL 向量表页 / 控制区主份 / BL 代码区 一律拒绝擦除 */
    return OTA_FLASH_ERR_PROTECT;
  }
  return BL_FlashEraseRangeInternal(addr, size, false);
}

/**
  * @brief  写入一段数据（公开接口，绝对保护 BL 自身与控制区）
  * @param  addr 起始地址（必须 8 字节对齐）
  * @param  data 数据
  * @param  len  字节数（内部向上补齐到 8 的倍数，尾部以 0xFF 填充）
  */
OTA_FlashResult BL_FlashWrite(uint32_t addr, const uint8_t *data, uint32_t len)
{
  if (addr < OTA_SLOT_A_ADDR)
  {
    return OTA_FLASH_ERR_PROTECT;
  }
  return BL_FlashWriteInternal(addr, data, len, false);
}

/**
  * @brief  受控旁路擦除（额外放行两个控制区页，仅 bl_param.c 使用）
  */
OTA_FlashResult BL_FlashEraseRangeEx(uint32_t addr, uint32_t size, bool allowCfg)
{
  if (!allowCfg && (addr < OTA_SLOT_A_ADDR))
  {
    return OTA_FLASH_ERR_PROTECT;
  }
  return BL_FlashEraseRangeInternal(addr, size, allowCfg);
}

/**
  * @brief  受控旁路写入（额外放行两个控制区页，仅 bl_param.c 使用）
  */
OTA_FlashResult BL_FlashWriteEx(uint32_t addr, const uint8_t *data, uint32_t len, bool allowCfg)
{
  if (!allowCfg && (addr < OTA_SLOT_A_ADDR))
  {
    return OTA_FLASH_ERR_PROTECT;
  }
  return BL_FlashWriteInternal(addr, data, len, allowCfg);
}

/**
  * @brief  回读校验：逐字节比对 Flash 与 RAM
  * @retval OTA_FLASH_OK / OTA_FLASH_ERR_VERIFY / OTA_FLASH_ERR_RANGE / OTA_FLASH_ERR_PROTECT
  */
OTA_FlashResult BL_FlashVerify(uint32_t addr, const uint8_t *data, uint32_t len)
{
  uint32_t i;

  if ((data == NULL) || (len == 0U))
  {
    return OTA_FLASH_ERR_RANGE;
  }
  /* 【M5 修复】allowCfg 由 true 改为 **false**。
   * 本函数只**读** Flash（不改写），本身不存在"擦写控制区"的风险；传 true 的真正
   * 危害是它**放行了控制区两个页的地址范围**，于是调用方一个笔误（例如把
   * receivedLen 算错、或addr 落在 BL 区）就会让"绝对保护 BL 向量表页与控制区"
   * 这条契约在读路径上被静默绕过 —— 典型的"看似有保护、实际被绕过"。
   * 头文件 bl_flash_if.h 对本函数的承诺是"任何 < OTA_SLOT_A_ADDR 的地址一律
   * PROTECT"，这里必须让实现与承诺一致。真正的控制区回读不需要走本函数。 */
  if (!BL_FlashRegionAllowed(addr, len, false))
  {
    return OTA_FLASH_ERR_PROTECT;
  }

  for (i = 0U; i < len; i++)
  {
    if (*(volatile const uint8_t *)(addr + i) != data[i])
    {
      return OTA_FLASH_ERR_VERIFY;
    }
  }

  return OTA_FLASH_OK;
}

/**
  * @brief  按 32 位字回读
  *
  * 【M4 修复 · 2026-10-07】本函数已**删除**。
  *
  *   删除理由（原实现的三个问题）：
  *     1) 对 addr 与 words **完全无边界检查**。out 通常指向调用方栈缓冲，
  *        words 一旦被算错就是栈溢出 —— 而 Flash 越界读在 STM32G4 上会
  *        触发 BusFault（等同于跳进 HardFault），没有任何优雅退路；
  *     2) 返回 void，**无法报错**，调用方也无从得知失败，只能"读了就是读了"；
  *     3) 与同文件其他所有接口的风格严重不一致 —— BL_FlashEraseRange /
  *        BL_FlashWrite / BL_FlashVerify / BL_FlashReadBytes 一律先做
  *        BL_FlashRegionAllowed 白名单校验，唯独它没有。
  *
*   为什么不"修好留着"：本函数**全工程零调用点**（Keil 链接器已将其标记为
 *   Removing，见 MDK-ARM/G474/BL.map:1138）。为一个死代码补边界检查，等于把
 *   "无人验证的分支"留在产物里，日后有人调用时反而以为它已被验证过。
 *   零调用点 + 有隐患 + 本模块已无带保护的读接口，删除优于加固。
 *
 *   若将来确实需要按字读回 Flash：本模块**只读不写**的场景，直接用
 *   `*(volatile const uint32_t *)addr` 即可（Flash 支持直接地址读，无需本模块），
 *   但调用方必须自己保证地址落在目标区内 —— 镜像头/配置区的读取方
 *   （bl_image.c / bl_param.c）就是这么做的，且它们有各自的槽/页白名单校验。
  */

/**
  * @brief  CRC32 自检入口（task-2 F 节不变量 10）
  * @retval true=通过；false=实现或契约常量被破坏，main 必须打印 "BL: crc self-test FAIL"
  * @note   契约常量本身在此再做一次编译期锁死，避免头文件被误改而静默通过
  */
#if (OTA_CRC32_CHECK_VECTOR != 0xCBF43926UL)
#error "OTA_CRC32_CHECK_VECTOR 必须为 0xCBF43926（CRC-32/ISO-HDLC）"
#endif

bool BL_FlashSelfTest(void)
{
  return BL_Crc32SelfTest();
}
