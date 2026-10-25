# 分层 RAG 实施验证结果

执行日期：2026-10-25。运行环境：macOS（aarch64）、Python 3.12.14、uv 0.12.13、`.venv` 已按
`uv.lock` 重建。下列结论只来自本机实际执行的命令与测试输出；未执行项明确标为未执行，
不记为通过。

## 1 环境与依赖（T001）

| 项目 | 结果 |
| --- | --- |
| `.python-version` | `3.12.14`（已固定） |
| `pyproject.toml` 版本范围 | `>=3.12,<3.13`，与固定版本一致 |
| 依赖声明变更 | 无（未新增第三方依赖） |
| `uv.lock` | 未修改；依赖图与实现前一致 |
| `python --version`（`.venv`） | `Python 3.12.14` |

## 2 自动化测试（T021、T032、T044、T056、T059）

命令：

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m pytest -q tests/unit tests/contract tests/integration -m 'not live and not embedding'
# → 520 passed, 4 deselected（live/embedding 标记项被显式排除）
```

结果：**523 passed, 1 skipped**（21.0s）。唯一跳过项是既有
`tests/integration/test_semantic_ingest.py:212`（本地未预下载固定 revision 的向量模型，
真实质量验证未执行），与本次功能无关。

按阶段统计（新增资料包相关测试）：

| 阶段 | 测试文件 | 结果 |
| --- | --- | --- |
| US1 + Foundational | `tests/contract/test_markdown_package.py`、`tests/unit/test_markdown_card_boundaries.py`、`tests/integration/test_character_package_build.py`、`tests/integration/test_package_migrations.py`、`tests/unit/test_package_errors.py` | 76 passed |
| US2 | `tests/integration/test_package_dialogue.py`、`tests/unit/test_package_output_policy.py` | 24 passed |
| US3 | `tests/unit/test_package_state_transitions.py`、`tests/contract/test_package_selection.py`、`tests/integration/test_package_context_retrieval.py` | 35 passed |
| US4 + 跨链路 | `tests/contract/test_package_index_cli.py`、`tests/integration/test_package_activation.py`、`test_package_evaluation.py`、`test_package_readiness.py`、`test_package_qq_regression.py`、`test_package_cli_flow.py` | 49 passed |

普通测试全部离线：使用替代编码器（字符 n-gram 话题向量）、`SubstituteChatModel` 与
`FakeQQTransport`，不下载模型、不读取真实账号、不产生付费调用。

## 3 资料完整性（SC-001、SC-002）

`config/character-package.json` + `doc/乾纱凪角色设定整理版/` 的实际解析结果：

| 指标 | 期望 | 实际 |
| --- | --- | --- |
| included 来源 | 6 | 6（01–06） |
| excluded 材料 | 2 | 2（00、07，不复制进快照） |
| 常驻材料 | 3 | `sana_core`、`sana_output`、`sana_emotion_general` |
| 完整卡 | 26 | emotion_policy 1 + emotion_rule 6 + scene_rule 6 + background 3 + appearance 1 + profile 2 + dialogue_example 7 |
| 编码卡 | 25 | 25（状态总则仅常驻） |
| 00/07 内容泄入 | 0 | 0（`test_excluded_materials_never_enter_generation_payload`） |
| 重复导入一致性 | 结构与来源哈希一致 | `structure_hash`、`source_set_hash`、`bundle_hash` 一致，仅 `index_id` 不同 |
| 四类故障拒绝 | 重复 ID、缺字段、缺文件、超容量 | 分别返回 `duplicate_card_id`、`package_metadata_invalid`、`package_file_missing`、`card_input_over_limit` |

真实 CLI 执行（隔离运行目录）：

```bash
sana-bot index parse --format markdown-package --source config/character-package.json \
  --output <tmp>/structures --runtime-dir <tmp>/runtime
# → resident_count=3, issues=[], approval_status=pending

sana-bot index review --structure <STRUCTURE_ID> --runtime-dir <tmp>/runtime
# → 26 卡逐卡条件/来源/字符数可见, approval=null

sana-bot index approve --structure <STRUCTURE_ID> --catalog config/character-package.json \
  --reviewer maintainer --runtime-dir <tmp>/runtime
# → approved_card_count=26, still_required=[build, calibrate, evaluate, approve-evaluation]
```

构建在本机未产生真实快照：见第 4 节。

## 4 真实编码与容量（T060，未执行）

```bash
sana-bot index build --structure <STRUCTURE_ID> --output <tmp>/indexes --runtime-dir <tmp>/runtime
# → exit 3, reason_code=embedding_revision_missing
```

原因：本机未配置固定 `EMBEDDING_REVISION`，也没有缓存该 revision 的本地向量模型；
按宪章与规格，缺少可信编码器时构建必须失败，不能改用启发式计数。
因此**逐卡真实 token 计数、真实 25 向量产物与实际 revision 未执行**，
详见 [model-results.md](model-results.md)。

替代编码器（仅测试用，字符 n-gram 话题向量 + 字符级 tokenizer）下的构建证据：
25 行向量、维度 512、逐卡 token 计数 39–381（上限 512）、封存文件 9 项、
`bundle_hash` 与 `snapshot_hash` 可重算且与 `cards.json`/`residents.json` 一致。

## 5 条件选择与校准（SC-003，替代依赖）

样本：`tests/fixtures/character_package/retrieval.jsonl`（36 正例 + 12 条件负例，执行前冻结）。

| 指标 | 门槛 | 实际（替代编码器） |
| --- | --- | --- |
| 正例命中率 | ≥ 90% | **100%**（37/37 期望卡命中） |
| 不合条件卡采用数 | 0 | **0** |
| 选定门槛 | 由校准产出 | 0.25 |
| 校准未改封存哈希 | 是 | `snapshot_hash`、`bundle_hash` 不变（`test_threshold_does_not_change_bundle`） |

负例覆盖公共场景、未知场景与 `medium=text`：private-only 的情绪卡与示例均未采用，
证据记录 `condition_rejected`。真实编码器下的校准未执行（需第 4 节前置条件）。

## 6 对话与输出（SC-004～SC-006，替代依赖）

- 空检索/低相关仍可正常生成：`test_empty_retrieval_still_generates_normally` 通过，
  结果为 `ready_without_references` 且实际发送成功。
- 三份常驻每轮完整注入：`test_turn_injects_all_three_residents` 通过。
- 用户消息不能升级为配置：`test_user_message_cannot_upgrade_to_configuration` 通过。
- 关系冲突受控失败且不写业务账本：`test_relationship_mismatch_fails_without_writing_ledger` 通过。
- 输出规范新增四类规则（代写用户动作/台词、示例当记忆、默认同地）均有独立用例，
  且仅在 `strict_package_rules` 下生效，旧格式行为不变。
- 评估框架：`index evaluate` 实际产出 48 个样本的逐轮产物（输入/回复/决策/选中卡/违规项），
  报告初始 `pending_review`；`operations` 记为 `not_run`；仅 `offline` 报告不能启用。

真实 DeepSeek 生成与人工逐段评分**未执行**（无凭据、无评审），因此 SC-004/SC-005/SC-006
的真实质量结论**尚未取得**，见 [model-results.md](model-results.md)。

## 7 切换与恢复（SC-007）

`tests/integration/test_package_activation.py` 覆盖：

| 演练 | 结果 |
| --- | --- |
| 未验收启用被拒 | 通过（`package_report_missing` / `package_offline_only` / `package_calibration_missing`） |
| 成功切换 | 通过（唯一 active + `activation_audit` 记录 expected/calibration/report_hash） |
| 处理中切换 | 通过（`test_switch_keeps_in_flight_reference_valid`：清缓存后旧引用仍可用） |
| 恢复旧版 | 通过（`rollback` 与 `activate` 共用验证器） |
| `expected-active` 冲突 | 通过（`active_version_conflict`，原 active 保留） |
| 篡改文件 | 通过（`bundle_hash_mismatch`） |
| 旧报告用于新包 | 通过（`package_report_missing`） |
| 业务数据不变 | 通过（user/turn/inbound 计数与 relationship 表前后一致） |

每轮混用新旧资料次数为 0：本轮上下文固定 `index_id`/`bundle_hash`/`policy_hash`/
`calibration_id`，切换后仅后续轮次读取新包。

## 8 诊断定位（SC-008）

`retrieval_evidence` 每轮记录 `index_id`、`snapshot_hash`、`bundle_hash`、`catalog_hash`、
`resident_ids`、逐卡证据（条件、分数、选中/排除原因、`budget_excluded`）与最终决策，
并关联 `turn_id`。测试断言见 `test_evidence_records_versions_and_decisions`。
「5 个问题各 ≤3 分钟定位」是人工操作指标，**未执行**（见 [qq-results.md](qq-results.md)）。

## 9 QQ 沙箱（T061，未执行）

真实收发、C2C 文本权限与回调联调**未执行**：本机没有 QQ 应用凭据，也没有沙箱用户。
替代链路证据：去重、拒发、uncertain、会话隔离、`msg_seq ≤ 4` 均通过
`tests/integration/test_package_qq_regression.py`。详见 [qq-results.md](qq-results.md)。

## 10 未执行项汇总

| 项目 | 状态 | 原因 |
| --- | --- | --- |
| 真实编码器逐卡 token 计数与 25 真实向量 | 未执行 | 未配置固定 revision / 未缓存模型 |
| 真实 DeepSeek 生成与 24 段人工评分 | 未执行 | 无凭据、无人工评审 |
| 5 类切换演练在真实运行库执行 | 未执行 | 仅在隔离测试库演练 |
| 维护者 5 问题定位计时 | 未执行 | 需人工操作 |
| QQ 真实收发与权限核对 | 未执行 | 无应用凭据与沙箱用户 |
| 生产启用/恢复操作 | 未执行 | 需上述真实证据与维护者批准 |

## 11 需求与成功标准覆盖

| 需求 | 证据 | 状态 |
| --- | --- | --- |
| FR-001 六份内容来源、00/07 不进生成 | `config/character-package.json`、`test_parse_command_writes_review_materials`、`test_excluded_materials_never_enter_generation_payload` | 通过（离线） |
| FR-002 三份常驻每轮生效、缺失不得启用 | `test_turn_injects_all_three_residents`、`test_missing_resident_is_not_ready` | 通过（替代依赖） |
| FR-003 完整卡、吃醋三级与恢复不拆散 | `test_jealous_card_keeps_three_levels_and_recovery`、`test_multiline_example_turns_and_continuations` | 通过 |
| FR-004 用途分类、无说话者不推断独白 | `test_no_card_is_inferred_as_monologue`、`test_card_type_must_match_source_role` | 通过 |
| FR-005 校验并阻止半包 | `test_missing_metadata_field_blocks_enablement` 等 6 个用例 | 通过 |
| FR-006 每来源与每卡可追溯 | `test_every_card_keeps_full_body`、`test_unicode_positions_use_code_points` | 通过 |
| FR-007 重复导入一致、不静默改写 | `test_repeated_build_is_logically_identical`、`test_repeated_parse_is_consistent` | 通过 |
| FR-008 结合上下文/前态/关系/场景选取 | `test_light_jealousy_escalates_to_medium_only_from_light`、`test_comforting_recovers_across_turns_and_keeps_same_card` | 通过 |
| FR-009 公共/独处/未知区分 | `test_unknown_scene_does_not_satisfy_private_condition`、`test_public_scene_excludes_private_only_references` | 通过 |
| FR-010 保留触发/升级/例外/恢复 | `test_real_hurt_does_not_escalate_to_sharp_anger`、`test_relief_moves_from_hurt_to_secure` | 通过 |
| FR-011 规则协调顺序 | `test_state_card_is_selected_independent_of_vector_score`、`package_extra_rules` 结构 | 通过 |
| FR-012 示例只作表达参考 | `test_references_are_typed_and_not_instructions`、`test_examples_do_not_create_user_facts` | 通过 |
| FR-013 未命中仍可交流、未知保持未知 | `test_empty_retrieval_still_generates_normally` | 通过（替代依赖） |
| FR-014 不编造未定义事实、用户间不串用 | `test_two_users_are_isolated`、`test_examples_do_not_create_user_facts` | 通过 |
| FR-015 关系不匹配受控失败 | `test_relationship_mismatch_fails_without_writing_ledger` | 通过 |
| FR-016 第一人称/自主权/无句号/纯台词 | `tests/unit/test_package_output_policy.py` 全部用例 | 通过 |
| FR-017 维护者可核对记录、不泄密 | `retrieval_evidence` 记录 + `test_evidence_records_versions_and_decisions`、`redact_details` | 通过 |
| FR-018 先待验收、旧结论不冒用 | `test_offline_only_report_cannot_activate_normally`、`test_new_package_cannot_reuse_old_report` | 通过 |
| FR-019 切换一致性与恢复、不清业务数据 | `test_switch_keeps_in_flight_reference_valid`、`test_business_data_unchanged_after_switch`、`test_rollback_restores_previous_version` | 通过 |
| FR-020 无独白素材明确禁用且不阻塞 | `test_monologue_disabled_does_not_block_reply`、`test_monologue_opportunity_skipped_when_disabled` | 通过 |
| FR-021 既有 QQ 链路与独立核对 | `test_package_qq_regression.py`（7 用例） | 通过（替代依赖）；真实收发未执行 |
| FR-022 启用前提交三类结果、offline/live 分开 | `index evaluate` + `approve-evaluation` 门槛、`test_approve_requires_artifacts_and_passed_thresholds` | 通过 |

| 成功标准 | 状态 |
| --- | --- |
| SC-001 100% 覆盖与零泄入 | 通过（离线） |
| SC-002 重复导入一致 + 四类故障拒绝 | 通过（离线） |
| SC-003 ≥90% 命中、0 违规采用 | 通过（替代编码器 100% / 0） |
| SC-004 24 例 ≥23 相关 | 框架通过（替代模型）；真实生成未执行 |
| SC-005 24 段 ≥22 通过、严重错误 0 | 框架通过（替代模型）；人工评分未执行 |
| SC-006 输出格式全部通过 | 规则检查通过；真实生成未执行 |
| SC-007 五类演练 + 业务数据不变 | 通过（隔离库） |
| SC-008 3 分钟内定位 | 数据结构就绪；人工计时未执行 |
