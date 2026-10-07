/**
  ******************************************************************************
  * @file    bl_param.c
  * @brief   控制区（OTA_Cfg）双副本读写实现 —— 掉电安全的核心
  *
  * 物理布局（契约 ota_common.h）：
  *   页0 = 0x08000000（PRIMARY）：页首 [0, OTA_CFG_VECTAB_BYTES) 是 **BL 自己的向量表**，
  *                                副本0 @ +OTA_CFG_SLOT_OFFSET(0x1D8)、副本1 @ +0x400
  *   页1 = 0x08040000（MIRROR） ：副本0 @ +0x1D8、副本1 @ +0x400
  *   共 4 个候选。
  *
  * 写入算法（task-2 D 节，四步不可省）：
  *   [PARAM-STEP-1] 把目标页**整页**读入 2KB RAM 暂存（s_pageBuf）
  *   [PARAM-STEP-2] 擦除该页（Flash 只能按 2KB 页擦）
  *   [PARAM-STEP-3] 擦后**整页恢复**：把页镜像里除"本次要写的那一份"以外的内容
  *                  按 8 字节双字原样写回（从地址 0 递增 -> 向量表最先落盘）
  *   [PARAM-STEP-4] 写入新值（seq = 当前 seq+1，CRC 已重算）
  *   最后回读校验：magic/CRC 有效且内容逐字节一致，否则返回 false（不静默）
  *
  * 两条血线（Lead 已写入 docs/01）：
  *   1) 写目标选择：取"最旧的有效副本所在位置"，但**绝不擦掉唯一最新副本所在页**；
  *      若最旧与最新同页，则改写到另一物理页的副本0 —— 保证任何时刻 Flash 中
  *      至少有一份有效配置。
  *   2) 页0 与 BL 向量表同页：擦页0 前必须整页暂存，擦后**先于**配置副本回写向量表
  *      [0, OTA_CFG_VECTAB_BYTES)。
  *   ⚠️ 血线 3（2026-10-05 补，代价是板子变砖）：**页 0 里绝不允许有 BL 代码**。
  *      STEP-3 过去只回写"向量表 + 另一份副本"，于是当 bl.sct 把代码铺进页 0 时，
  *      第一次写控制区就擦掉了 BL 自己的复位向量代码（实测复位向量 = 0x08000325，
  *      而 0x08000324 处读回 16 字节全为 0xFF）。
  *      现在 STEP-3 改为**整页恢复**（第二道防线），第一道防线是链接布局
  *      `bl.sct`（页 0 只放向量表），由 `ota/scripts/bl_layout.py` + `audit_bl_layout.py` 守。
  *
* 残余风险（见 BL_BUILD.md §控制区）：擦页0 到回写向量表之间存在约几十毫秒窗口，
 * 期间掉电会丢失 BL 向量表（需 SWD 救回）。把 BL_PARAM_PRIMARY_WRITE 置 0
 * 可彻底避免（配置只在 MIRROR 页两副本间轮转），代价是失去跨页冗余。
 *
 * 【2026-10-07 修复 H2/H3 后的精确表述】
 *   原注释说"置 0 可彻底避免"，修复前这句话**不成立**，因为存在两条绕过路径：
 *     - H3：全部副本无效时的回退路径无条件写 PRIMARY 页（典型触发场景=全新芯片
 *           首次上电，恰是该宏最需要生效的时刻）；
 *     - H2：置 0 时 *off 恒为副本0，副本1 永不被写，双副本退化为单副本。
 *   修复后（见 BL_ParamSelectTarget 的说明）两条路径均已封堵，"置 0 则绝不碰
 *   PRIMARY 页"名副其实。剩余代价需明确：置 0 时冗余**降级为同页两副本 ping-pong**
 *   （不再跨页），故 BL_ParamStore 的关中断窗口仅覆盖 MIRROR 页，不涉及向量表，
 *   变砖风险为零 —— 这是该宏真正的价值所在。
  ******************************************************************************
  */
#include "bl_param.h"
#include "bl_flash_if.h"
#include "bl_crc32.h"
#include <stddef.h>
#include <string.h>

/*--------------------------------------- 模块配置 ----------------------------------------*/
#define BL_PARAM_CAND_NUM         4U    /* 2 页 × 页内 2 份副本 */
#define BL_PARAM_PRIMARY_WRITE    1     /* 1=允许擦写 PRIMARY 页（跨页冗余，按 Lead 裁定）；0=只写 MIRROR 页 */

/*--------------------------------------- 模块内部变量 ------------------------------------*/
static uint8_t s_pageBuf[OTA_CFG_PAGE_SIZE];   /* 目标页 2KB 暂存（static：RAM 2KB） */
static OTA_Cfg s_cfg;                          /* 当前有效配置（RAM 镜像） */
static bool    s_cfgValid;                     /* 是否找到过有效副本 */

/* 4 个候选位置：索引 0/1 = PRIMARY 页副本0/副本1；2/3 = MIRROR 页副本0/副本1 */
static const uint32_t s_candPage[BL_PARAM_CAND_NUM] =
{
  OTA_CFG_PRIMARY_ADDR, OTA_CFG_PRIMARY_ADDR, OTA_CFG_MIRROR_ADDR, OTA_CFG_MIRROR_ADDR
};
static const uint32_t s_candOff[BL_PARAM_CAND_NUM] =
{
  OTA_CFG_SLOT_OFFSET, OTA_CFG_SLOT_MIRROR_OFF, OTA_CFG_SLOT_OFFSET, OTA_CFG_SLOT_MIRROR_OFF
};

/*--------------------------------------- 内部工具 ----------------------------------------*/
/**
  * @brief  副本内容是否"语义可信"
  * @note   【L3 修复，2026-10-07】
  *   CRC 只能证明"这48 字节没被传错"，**不能证明"这 48 字节是本固件写出来的"**。
  *   一份 CRC 正确但 state 越界（例如 state = 0xFF）的配置在过去会被直接采纳，
  *   然后被 BL_ParamSlotOfState() 的 default 分支静默按 Slot A 处理 —— 于是设备
  *   可能跳进**正在被刷写**的那个槽。这属于"看起来合法、语义错误"的危险输入。
  *
  *   契约定义的 OTA_ST_STATE_MAX 在修复前全工程**零引用**，正说明这项校验缺失。
  *   另需注意 trialLeft 的语义：仅 TRIAL_x 状态才有效，其余状态必须为 0，
  *   否则会出现"非试运行状态却带着剩余次数"的诡异配置。
  */
static bool BL_ParamContentSane(const OTA_Cfg *cfg)
{
  if (cfg->state >= OTA_ST_STATE_MAX)
  {
    return false;                              /* state 越界：不可能由本固件写出 */
  }

  if ((cfg->state == OTA_ST_TRIAL_A) || (cfg->state == OTA_ST_TRIAL_B))
  {
    if (cfg->trialLeft > OTA_TRIAL_BOOT_LIMIT)
    {
      return false;                            /* 试运行剩余次数超过上限 */
    }
  }
  else if (cfg->trialLeft != 0U)
  {
    return false;                              /* 非试运行状态不应带trialLeft */
  }

  return true;
}

/**
  * @brief  从 Flash 原样读出一个 OTA_Cfg（按 u32 读，避免对齐问题）
  */
static void BL_ParamReadRaw(uint32_t addr, OTA_Cfg *out)
{
  uint32_t  i;
  uint32_t *p = (uint32_t *)out;

  for (i = 0U; i < (uint32_t)(sizeof(OTA_Cfg) / sizeof(uint32_t)); i++)
  {
    p[i] = *(volatile const uint32_t *)(addr + (i * 4U));
  }
}

/**
  * @brief  结构 CRC：对 [0, offsetof(OTA_Cfg, crc)) 计算
  */
static uint32_t BL_ParamCalcCrc(const OTA_Cfg *cfg)
{
  return BL_Crc32((const uint8_t *)cfg, (uint32_t)offsetof(OTA_Cfg, crc));
}

/**
  * @brief  读并校验某个候选位置
  * @retval true=magic、结构 CRC 均正确**且内容语义可信**
  * @note【L3 修复】在 magic + CRC 之后追加 BL_ParamContentSane()。
  *   这一处是"副本是否有效"的唯一判定入口（载入、选写目标、回读校验三处共用），
  *   因此在这里加内容校验即可全覆盖，无需在各调用点分别防御。
  */
static bool BL_ParamIsValidAt(uint32_t page, uint32_t off, OTA_Cfg *out)
{
  BL_ParamReadRaw(page + off, out);

  if (out->magic != OTA_CFG_MAGIC)
  {
    return false;
  }
  if (out->crc != BL_ParamCalcCrc(out))
  {
    return false;
  }
  /* CRC 对得上只说明"没传错"，不说明"内容合理" —— 这里补语义校验 */
  if (!BL_ParamContentSane(out))
  {
    return false;
  }
  return true;
}

/**
  * @brief  构造"全部副本无效"时的默认配置（不落盘，等真正需要写时再落）
  */
static void BL_ParamSetDefault(OTA_Cfg *cfg)
{
  (void)memset(cfg, 0, sizeof(OTA_Cfg));
  cfg->magic     = OTA_CFG_MAGIC;
  cfg->seq       = 0U;
  cfg->state     = OTA_ST_IDLE_A;
  cfg->trialLeft = 0U;
  cfg->lastError = OTA_ERR_NONE;
  cfg->crc       = BL_ParamCalcCrc(cfg);
}

/**
  * @brief  选择本次写入的物理位置
  * @note   血线1：写"最旧的有效副本所在位置"，但绝不擦掉唯一最新副本所在页
  *
  * 【H2/H3 修复说明】
  *   检查报告指出两处逻辑缺陷，均已修，逐条说明如下：
  *
  *   H3 —— "全部副本无效"回退路径写 PRIMARY 页
  *     原代码在 oldestIdx < 0 时无条件 `*page = OTA_CFG_PRIMARY_ADDR`，**完全无视
  *     BL_PARAM_PRIMARY_WRITE**。这直接毁掉该宏的全部意义：该宏存在的唯一目的就是
  *     "彻底不碰 PRIMARY 页"（该页与 BL 向量表同页，擦写有变砖风险，见文件头血线 2/3）。
  *     而"全部副本无效"最典型的触发场景就是**全新芯片首次上电**（Flash 全 0xFF）——
  *     也就是说 Setting 该宏为 0 之后，第一次写配置照样擦 PRIMARY 页、照样承担
  *     向量表丢失风险，正是最该避免的时刻。现改为按宏选择落点。
  *
  *   H2 —— PRIMARY_WRITE=0 时副本1 永远不会被写，双副本退化为单副本
  *     该分支下所有有效副本都在 MIRROR 页，故 oldestIdx 与 newestIdx **恒同页**，
  *     于是原代码永远走 line 168 分支，且 *off 恒为 OTA_CFG_SLOT_OFFSET（副本0）。
  *     后果：副本1（+0x400）永远不写、永远无效，血线1 所依赖的"另一份有效副本
  *     兜底"根本不存在 —— 每次写都是擦掉唯一幸存者，只靠 STEP-3 整页恢复续命。
  *     这不是"少一份冗余"，而是**双副本机制名存实亡**。
  *     现改为：同页时选"**非最新**的那一份"作写入目标，从而形成真正的 ping-pong：
  *       写 copy0 -> STEP-3 恢复 copy1 -> copy1 变最新
  *       下次写 copy1 -> STEP-3 恢复 copy0 -> copy0 变最新
  *     于是任何时刻"擦除窗口外"都恒有**两份**有效配置，血线1 才真正成立。
  */
static void BL_ParamSelectTarget(uint32_t *page, uint32_t *off)
{
  OTA_Cfg  cand;
  int32_t  oldestIdx = -1;
  int32_t  newestIdx = -1;
  uint32_t oldestSeq = 0U;
  uint32_t newestSeq = 0U;
  uint32_t i;

  for (i = 0U; i < BL_PARAM_CAND_NUM; i++)
  {
#if (BL_PARAM_PRIMARY_WRITE == 0)
    if (s_candPage[i] == OTA_CFG_PRIMARY_ADDR)
    {
      continue;                                    /* 不擦 PRIMARY 页：向量表零风险 */
    }
#endif
    if (BL_ParamIsValidAt(s_candPage[i], s_candOff[i], &cand))
    {
      if ((oldestIdx < 0) || (cand.seq < oldestSeq))
      {
        oldestSeq = cand.seq;
        oldestIdx = (int32_t)i;
      }
      if ((newestIdx < 0) || (cand.seq > newestSeq))
      {
        newestSeq = cand.seq;
        newestIdx = (int32_t)i;
      }
    }
  }

  if (oldestIdx < 0)
  {
    /* 全部无效：没有任何需要保全的副本，选一个当前策略允许的落点即可。
     * 【H3 修复】原代码无条件写 PRIMARY 页，无视 BL_PARAM_PRIMARY_WRITE=0 ——
     * 而本分支最典型的触发场景恰是全新芯片首次上电（Flash 全 0xFF），正是该宏
     * 最需要生效的时刻。现在按宏分流：置 0 时只写 MIRROR 页。*/
#if (BL_PARAM_PRIMARY_WRITE == 0)
    *page = OTA_CFG_MIRROR_ADDR;
#else
    *page = OTA_CFG_PRIMARY_ADDR;
#endif
    *off  = OTA_CFG_SLOT_OFFSET;
    return;
  }

  if (s_candPage[oldestIdx] == s_candPage[newestIdx])
  {
    /* 最旧与最新同页：写该页会连唯一最新副本一起擦掉 -> 改写到另一物理页 */
#if (BL_PARAM_PRIMARY_WRITE != 0)
    *page = (s_candPage[oldestIdx] == OTA_CFG_PRIMARY_ADDR) ? OTA_CFG_MIRROR_ADDR : OTA_CFG_PRIMARY_ADDR;
#else
    *page = OTA_CFG_MIRROR_ADDR;                    /* 只允许 MIRROR 页（血线1 降级版） */
#endif
    /* 【H2 修复】原代码此处恒取 OTA_CFG_SLOT_OFFSET（副本0），导致副本1 永远不被写，
     * 双副本退化为单副本。改为选"非最新"的那一份，使同页两副本真正 ping-pong：
     *   写 copy0 -> STEP-3 恢复 copy1 -> copy1 成为最新
     *   再写 copy1 -> STEP-3 恢复 copy0 -> copy0 成为最新
     * 效果：擦除窗口之外恒有两份有效配置，血线1 才真正成立。*/
    *off  = (s_candOff[newestIdx] == OTA_CFG_SLOT_OFFSET)
             ? OTA_CFG_SLOT_MIRROR_OFF
             : OTA_CFG_SLOT_OFFSET;
    return;
  }

  *page = s_candPage[oldestIdx];
  *off  = s_candOff[oldestIdx];
}

/*--------------------------------------- 对外接口 ----------------------------------------*/
/**
  * @brief  载入配置（4 个候选取 seq 最大且有效者）
  */
bool BL_ParamLoad(OTA_Cfg *out)
{
  OTA_Cfg  cand;
  OTA_Cfg  best;
  uint32_t bestSeq = 0U;
  bool     found   = false;
  uint32_t i;

  (void)memset(&best, 0, sizeof(best));

  for (i = 0U; i < BL_PARAM_CAND_NUM; i++)
  {
    if (BL_ParamIsValidAt(s_candPage[i], s_candOff[i], &cand))
    {
      if ((!found) || (cand.seq > bestSeq))
      {
        bestSeq = cand.seq;
        best    = cand;
        found   = true;
      }
    }
  }

  if (found)
  {
    s_cfg      = best;
    s_cfgValid = true;
  }
  else
  {
    BL_ParamSetDefault(&s_cfg);
    s_cfgValid = false;
  }

  if (out != NULL)
  {
    *out = s_cfg;
  }

  return found;
}

/**
  * @brief  落盘配置（四步 + 回读校验）
  */
bool BL_ParamStore(const OTA_Cfg *newCfg)
{
  OTA_Cfg  target;
  OTA_Cfg  back;
  uint32_t tgtPage  = OTA_CFG_PRIMARY_ADDR;
  uint32_t tgtOff   = OTA_CFG_SLOT_OFFSET;
  uint32_t i;
  uint32_t primask = 0U;                          /* 页0 擦写窗口的 PRIMASK 现场 */
  bool     ok = true;

  if (newCfg == NULL)
  {
    return false;
  }

  BL_ParamSelectTarget(&tgtPage, &tgtOff);

  /* 新值：seq 单调 +1，结构 CRC 由本函数重算（调用方不必关心）
   *
   * 【L4 修复 · 2026-10-07】seq 改为**饱和**递增，不许回绕。
   * 原实现是 `(s_cfg.seq + 1U)`，一旦 seq 达到 0xFFFFFFFF，加法自然回绕到 0，
   * 于是"取 seq 最大者"的载入逻辑会把这份**最新的**配置当成最旧的一份丢弃
   * —— 表现为"设备突然回到了很久以前的状态"。
   *
   * 触发条件需要 2^32 次落盘，实际不可能触及（Flash 寿命会先耗尽），因此本条
   * 定级为 LOW。但它的危害是**静默的状态回退**，且难以复现，饱和处理成本极低
   * （一次比较），没有理由不做。饱和后 seq 停在 0xFFFFFFFF，
   * 后续落盘由"写最旧副本"的策略保证仍能正确更新内容（仅失去"取最新"的能力，
   * 退化为"取第一个有效副本"，可接受）。*/
  target      = *newCfg;
  target.magic = OTA_CFG_MAGIC;
  if (s_cfgValid && (s_cfg.seq < 0xFFFFFFFFU))
  {
    target.seq = s_cfg.seq + 1U;
  }
  else if (s_cfgValid)
  {
    target.seq = 0xFFFFFFFFU;                /* 已饱和：保持不变，不再回绕 */
  }
  else
  {
    target.seq = 0U;/* 首次落盘：无前序 seq，从 0 起 */
  }
  target.crc   = 0U;
  target.crc   = BL_ParamCalcCrc(&target);

  /* [PARAM-STEP-1] 读取目标页整页到 RAM 暂存：
   *                擦除按页进行，会连带擦掉同页另一份副本（PRIMARY 页还有 BL 向量表） */
  for (i = 0U; i < OTA_CFG_PAGE_SIZE; i++)
  {
    s_pageBuf[i] = *(volatile const uint8_t *)(tgtPage + i);
  }

  /* 血线2 的致命细节：擦 0x08000000 页会把 **BL 自己的向量表**变成 0xFFFFFFFF，
   * 而 SysTick 每 1ms 就挂起一次 —— 擦除期间 CPU 因 Flash 忙被 stall，一旦总线释放，
   * 挂起的 SysTick 会立刻按"已被擦掉的向量表"取向量 -> HardFault -> lockup（且复位后
   * SP/PC 仍是 0xFFFFFFFF，等于变砖）。所以从擦除前到向量表回写完成必须全程关中断。
   * 窗口约 30ms（擦 25ms + 回写 472B 约 5ms），远小于 IWDG 3s。 */
  if (tgtPage == OTA_CFG_PRIMARY_ADDR)
  {
    primask = __get_PRIMASK();
    __disable_irq();
  }

  /* [PARAM-STEP-2] 擦除该页 */
  if (BL_FlashEraseRangeEx(tgtPage, OTA_CFG_PAGE_SIZE, true) != OTA_FLASH_OK)
  {
    ok = false;
  }

  /* [PARAM-STEP-3] 擦后恢复：把**页镜像里除"本次要写的那一份"以外**的内容原样写回。
   *
   * 为什么是"整页恢复"，而不是"只回写向量表 + 另一份副本"：
   *   擦除按 2KB 页进行，凡落在本页内的东西都会被擦掉；只恢复两样东西，
   *   等于假设"页内除了这两样什么都没有"。**这个假设曾经不成立** ——
   *   2026-10-05 的 bl.sct 把 BL 代码铺进了本页（ER_IROM1 = 0x08000000 + 0xB800），
   *   第一次写控制区就把 BL 自己的复位向量代码擦成 0xFF：
   *   复位向量 = 0x08000325，而 0x08000324 处读回 16 字节全为 0xFF —— 板子当场变砖。
   *   整页恢复后，即使链接布局再次出错，本函数也不会自杀（这是第二道防线，
   *   第一道是 bl.sct 里"页 0 只放向量表"，见 ota/scripts/bl_layout.py）。
   *
   * 顺序：从 0 起按 8 字节双字递增，因此**向量表最先落盘**（砖化窗口最小）。
   * 跳过区间：本次要写的新值所在的那一份 [tgtOff, tgtOff + sizeof(OTA_Cfg))，由 STEP-4 写入。
   * 只写"镜像里含非 0xFF 字节"的双字，避免对空区做无谓编程（空区写 0xFF 是白做工）。 */
  if (ok)
  {
    uint32_t pos = 0U;

    while ((pos < OTA_CFG_PAGE_SIZE) && ok)
    {
      uint32_t k;
      bool     blank = true;

      if ((pos + 8U > tgtOff) && (pos < (tgtOff + (uint32_t)sizeof(OTA_Cfg))))
      {
        pos = tgtOff + (uint32_t)sizeof(OTA_Cfg);  /* 让位给 STEP-4 */
        continue;
      }

      for (k = 0U; k < 8U; k++)
      {
        if (s_pageBuf[pos + k] != 0xFFU)
        {
          blank = false;
          break;
        }
      }

      if (!blank &&
          (BL_FlashWriteEx(tgtPage + pos, &s_pageBuf[pos], 8U, true) != OTA_FLASH_OK))
      {
        ok = false;                                /* 恢复失败：本页已毁，返回失败让上层告警 */
      }

      pos += 8U;
    }
  }

  /* 向量表与整页内容已恢复（或已确认失败），可以恢复中断了 */
  if ((tgtPage == OTA_CFG_PRIMARY_ADDR) && (primask == 0U))
  {
    __enable_irq();
  }

  /* [PARAM-STEP-4] 写入新值（长度用 sizeof(OTA_Cfg)，不写字面量） */
  if (ok)
  {
    if (BL_FlashWriteEx(tgtPage + tgtOff, (const uint8_t *)&target,
                        (uint32_t)sizeof(OTA_Cfg), true) != OTA_FLASH_OK)
    {
      ok = false;
    }
  }

  /* 回读校验：必须能按 magic/CRC 读出且与写入内容逐字节一致 */
  if (ok)
  {
    if (!BL_ParamIsValidAt(tgtPage, tgtOff, &back))
    {
      ok = false;
    }
    else if (memcmp(&back, &target, sizeof(OTA_Cfg)) != 0)
    {
      ok = false;
    }
    else
    {
      /* 内容一致，无需额外动作 */
    }
  }

  if (!ok)
  {
    return false;
  }

  s_cfg      = target;
  s_cfgValid = true;
  return true;
}

/**
  * @brief  当前配置（RAM 镜像）
  */
const OTA_Cfg *BL_ParamGet(void)
{
  return &s_cfg;
}

/**
  * @brief  由 state 求活动槽
  */
uint8_t BL_ParamSlotOfState(uint32_t state)
{
  switch (state)
  {
    case OTA_ST_IDLE_B:
    case OTA_ST_TRIAL_B:
      return 1U;

    case OTA_ST_IDLE_A:
    case OTA_ST_TRIAL_A:
    default:
      return 0U;                                   /* 非法 state 一律按 A 处理（保守） */
  }
}
