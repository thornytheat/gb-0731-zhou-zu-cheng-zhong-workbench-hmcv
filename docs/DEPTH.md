# 轴组称重工作台深度登记

- 规模为中型。园区地磅稽核员按轮道审查晚班触发，确认轴组，交给复秤岗核对异常车次。
- 核心规则是同轮道去抖后，以唯一对侧候选和轴距窗确认车轴，保留缺证依据。

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 采样验收 | 查看分段、物理行、乱序记录和坏行 | `GET /api/intake` | `app/modules/m1_intake/reader.py` `app/modules/m1_intake/service.py` | `tests/test_intake.py` |
| M2 传感器标定 | 按采样时刻核证，并复核重叠证书 | `GET /api/calibration` | `app/modules/m2_calibration/service.py` `app/modules/m2_calibration/certificates.py` | `tests/test_calibration.py` |
| M3 脉冲去抖 | 核对重送、回弹和冲突记录号 | `GET /api/debounce` | `app/modules/m3_debounce/identity.py` `app/modules/m3_debounce/service.py` | `tests/test_debounce.py` |
| M4 轴组重建 | 选择车次查看轮载、争配裁决、轴距与隔离证据 | `GET /api/axles` | `app/modules/m4_axles/resolver.py` `app/modules/m4_axles/service.py` `app/modules/m4_axles/spacing.py` | `tests/test_axles.py` `tests/test_contention.py` |
| M5 偏载判级 | 核查左重、右重和临界偏载 | `GET /api/balance` | `app/modules/m5_balance/thresholds.py` `app/modules/m5_balance/service.py` | `tests/test_balance.py` |
| M6 整车磅单勾稽 | 审核磅单版次、差额和待复秤原因 | `GET /api/reconcile` | `app/modules/m6_reconcile/tickets.py` `app/modules/m6_reconcile/service.py` | `tests/test_reconcile.py` |
| M7 称重批次 | 提交样例、重算、追溯并下载复核包 | `POST /api/batches` | `app/modules/m7_batches/service.py` `app/modules/m7_batches/export.py` `app/store.py` | `tests/test_batches.py` |

## 陷阱登记

| TRAP | 现象 | 朴素实现会怎样 | 本项目处理 | 处理位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-01 | `certificates.csv` 为 GB18030 | 中文证书读不出 | 严格解码回退 | `app/modules/m1_intake/decode.py` | `tests/test_calibration.py::test_gb18030_recovers_certificate_headers_and_ids` |
| TRAP-02 | `dirty-triggers.csv` 有 BOM | 记录号列失配 | 识别并剥离 BOM | `app/modules/m1_intake/decode.py` | `tests/test_intake.py::test_bom_header_uses_record_id` |
| TRAP-03 | 脏触发第 3–4 行为一条记录 | 备注被切成两行 | CSV 表增量迭代并保存起止物理行 | `app/modules/m1_intake/reader.py` | `tests/test_intake.py::test_multiline_note_keeps_physical_source_range` |
| TRAP-04 | 脏触发第 13 行重复表头 | 多算一条触发 | 表头分段并从触发账剔除 | `app/modules/m1_intake/reader.py` | `tests/test_intake.py::test_repeated_header_and_blank_do_not_inflate_raw_count` |
| TRAP-05 | 脏触发第 26 行多一列 | 字段错位 | 隔离原行 | `app/modules/m1_intake/service.py` | `tests/test_intake.py::test_extra_column_is_quarantined_at_source_line` |
| TRAP-06 | 脏样例有全角小数和千分位 | 数值丢失 | 先规格化，再精确解析 | `app/values.py` | `tests/test_intake.py::test_full_width_decimal_and_thousands_grouping` |
| TRAP-07 | 脏触发第 24 行是 N/A | 当作零重 | 原值留在隔离表 | `app/values.py` | `tests/test_intake.py::test_missing_sentinel_never_contributes_zero` |
| TRAP-08 | 车次 0007 | 前导零丢失 | 全流程文本车次 | `app/modules/m6_reconcile/tickets.py` | `tests/test_reconcile.py::test_latest_semantic_duplicate_and_inclusive_tolerance` |
| TRAP-10 | 明细叠加半分边界轮载 | 合计或边界失准 | Decimal 逐轮修正至 0.01 kg 后汇总 | `app/modules/m6_reconcile/service.py` | `tests/test_reconcile.py::test_decimal_rollup_matches_independently_summed_axles` |
| TRAP-11 | 脏触发和证书的时区写法不同 | 生效边界错判 | UTC 比较 | `app/values.py` | `tests/test_calibration.py::test_mixed_offsets_align_to_same_utc_clock` |
| TRAP-13 | 晚班触发次序错位 | 丢脉冲或误去抖 | 标记乱序，按时间排序去抖 | `app/modules/m3_debounce/service.py` | `tests/test_intake.py::test_out_of_order_sensor_row_is_retained_and_marked` |
| TRAP-14 | 脏触发第 45–46 行同记录号不同载荷 | 后写覆盖 | 两条都隔离且互相引用 | `app/modules/m3_debounce/identity.py` | `tests/test_debounce.py::test_same_id_conflict_rejects_both_records` |
| TRAP-15 | 脏触发混用斤、吨和千克 | 混加原始数值 | 按单位统一到千克再标定 | `app/values.py` | `tests/test_calibration.py::test_affine_calibration_keeps_mixed_unit_weights_consistent` |
| TRAP-18 | 空文件、空行、仅表头 | 输出无意义结果 | 三个明确失败码 | `app/modules/m1_intake/reader.py` | `tests/test_intake.py::test_empty_blank_and_header_only_have_distinct_errors` |
| TRAP-19 | 0019 两个左轮争同一右轮 | 贪心配对凭空造轴、只留一轴 | 整车枚举全部一对一方案，按成轴数/轴距/偏载/磅单证据取唯一最优，恢复两轴，落选左轮按缺证隔离 | `app/modules/m4_axles/resolver.py` `app/modules/m4_axles/service.py` | `tests/test_axles.py::test_whole_vehicle_evidence_recovers_two_axles_when_candidates_conflict` |
| TRAP-26 | 争配方案在整车证据上完全并列 | 按最近距离或记录号私自择一 | 证据分并列即判 `blocked`，轮载挂起、偏载不判级、差额再小也不核平 | `app/modules/m4_axles/resolver.py` | `tests/test_contention.py::test_tied_whole_vehicle_evidence_blocks_resolution` |
| TRAP-27 | 上游轮载更新后沿用旧车轴结论 | 局部改一轴、整车账错位 | 批次键含源摘要，输入变更即新批次，相关车轴/偏载/勾稽随整批重算 | `app/manifest.py` `app/modules/m7_batches/service.py` | `tests/test_batches.py::test_changed_source_rejects_rerun_without_rewriting_old_snapshot` |
| TRAP-28 | 同一输入重跑把已排除候选再计一次 | 隔离数与轴组在重跑间漂移 | 枚举与排序为确定性纯函数，争配台账和排除集合逐次稳定 | `app/modules/m4_axles/resolver.py` | `tests/test_contention.py::test_rerun_with_same_input_is_stable_and_keeps_exclusions` |
| TRAP-23 | 0007 触发恰好在前证书右端 | 一轮发给两版证书 | 生效区间左闭右开 | `app/modules/m2_calibration/service.py` | `tests/test_calibration.py::test_exact_handoff_uses_new_certificate_and_no_double_match` |
| TRAP-25 | `certificates.csv` 两张右轮证书叠期 | 随机取一张 | 隔离触发并带两张证书行 | `app/modules/m2_calibration/service.py` | `tests/test_calibration.py::test_overlap_isolates_with_both_certificates` |

## 对账规则

- 原始触发 = 有效触发 + 重复触发 + 隔离触发。空行、重复表头、磅单异常独立计数。
- 整车差额 = 静态总重 − 确认车轴的左右轮载合计。缺少有效磅单时差额留空。
- 争配未决车次的车轴以 `tentative` 计入有效轮载守恒（其轮载仍是有效采样），但偏载判级为 `blocked`、整车勾稽强制待复秤；落选与缺证轮载计入隔离，守恒账不重不漏。

## 已知不覆盖

- 输入限定配置目录内样例文件，按文件 1 MiB 和 4,000 条逻辑记录封顶。只支持带显式时区偏移和毫秒精度以内的采样时刻。
- 轴距用档案内的车次恒速推算。缺轮、歧义或异常轴距产生现场复秤待办，现场结果需在外部系统处置。争配挂起车次的最终取人复核结果同样在外部系统登记，本工作台只保留证据与候选方案。
