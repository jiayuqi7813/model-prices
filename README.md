# Model prices

A daily, public snapshot of model prices from [LiteLLM](https://github.com/BerriAI/litellm), [models.dev](https://models.dev), [Cursor’s official model pricing](https://cursor.com/docs/models-and-pricing), and [Devin’s official SWE-2 list pricing](https://docs.devin.ai/desktop/models.md). No API key, account, or paid inference request is needed.

Stable raw feed: [prices.json](https://raw.githubusercontent.com/jiayuqi7813/model-prices/main/prices.json)

```sh
curl -fsSL https://raw.githubusercontent.com/jiayuqi7813/model-prices/main/prices.json
```

## Data contract

`prices.json` is a flat map compatible with LiteLLM token-price fields. `_meta` is reserved metadata, not a model. Ignore keys beginning with `_` when enumerating models. `_meta.schema_version` is `1`; `_meta.generated_at` is UTC. Each required source records its URL, fetch time, SHA-256 of the fetched bytes, source entry count, and `status: "ok"`. These timestamps describe our fetch, not the upstream price update time.

- **LiteLLM first:** exact model keys and all original fields are preserved, including cache, priority/fast, long-context, audio, and other billing dimensions. Its `sample_spec` documentation example is excluded because its zeros are not actual model prices.
- **models.dev additions:** only missing `provider_id/model_id` keys are added. Existing keys are never merged or overwritten by models.dev, even when their prices are incomplete. This avoids guessing provider equivalence. For example, `openrouter/openai/gpt-5` and `openai/gpt-5` represent different serving providers. Model IDs already containing `/` still receive the provider prefix. No fuzzy aliases or canonical-model aliases are synthesized.
- **Cursor:** within the exact `cursor/` namespace, official rates take precedence over overlapping aggregate entries; `_meta.cursor_replaced_aggregate_keys` counts replacements. Official token pricing tables supply `cursor/<documented-model-id>` entries, including distinct Fast and 500k variants where Cursor documents IDs. Display names and the official source URL are preserved. These are base list / additional-usage estimates, not monthly subscription costs. Select the actual model ID and variant: Composer 2.5 defaults to Fast in the product, so its standard price must not be used for an unspecified speed. Auto and default routing have no fixed-price entries. Plan token surcharges, regional pricing, and conditional Max/Auto routing require account/request context and are excluded from the flat estimate. A dash in Cache Write means absent, never zero. IDs use an explicit mapping verified against official model pages and the [official public model catalog](https://cursor.com/docs-static/_next/static/immutable/chunks/2gzgn5h9nqkyy.js), rather than guessed names. New unmapped table labels are listed in `_meta.cursor_unmapped_names` and omitted until their IDs are verified; `_meta.cursor_pricing_rows`, `cursor_added`, and `cursor_replaced_aggregate_keys` expose coverage. This feed does not claim every Cursor model or account-specific tariff is covered. `_cursor_pricing.fast_model_id` links a standard entry to its documented Fast price when available. `short_context_model_id` links long-window variants back to the rate for shorter inputs. Grok 4.7 long-context rows apply above 256,000 input tokens; 500k is the context variant name, not its price threshold.
- **Devin:** `devin/swe-2-medium`, `devin/swe-2-high`, and `devin/swe-2-max` share the SWE-2 `public_list_price` explicitly stated in [official public documentation](https://docs.devin.ai/desktop/models.md). The parser reads only the self-serve list-price sentence, never the account-specific model table. `_devin_pricing` excludes subscription allowance, promotions, and enterprise discounts. IDs are verified from embedded public `model_uid` values in the [public model catalog](https://docs.devin.ai/desktop/models); no authenticated List Models API is called. Missing cache-write pricing remains absent. A missing or structurally unrecognized list-price statement fails the sync. Other Devin models remain outside this explicit coverage.
- **Units:** mapped token-price fields are USD per token. models.dev, Cursor, and Devin publish USD per million tokens, so mapped values are divided by 1,000,000. `_source_cost` retains its original cost object and units, including dimensions not mapped to LiteLLM fields. LiteLLM's non-token price fields retain their original units indicated by their names.
- **Unknown values:** missing, `null`, malformed, negative, or non-finite prices are never converted to zero. Original LiteLLM fields are retained, including incomplete records. Invalid models.dev values are not mapped. Unrecognized Cursor price cells fail the sync. Published numeric zeros remain zero. Consumers must validate the fields they use.
- **Computability:** `_price_status: "computable"` means both base input and output token rates are finite, nonnegative numbers, with booleans rejected. Otherwise it is `"unknown"`. This is a base token estimate, not a guarantee that every charge is covered. Consumers still need to select applicable cache, context, service-tier, modality, and other billing dimensions.
- **Provenance:** every model has `_price_provenance` with its source and source key; models.dev records also have the provider ID and original unit; Cursor and Devin records retain their official display name, source URL, original costs, and variant metadata. No record mixes prices from different sources or providers.

All four sources are required for each publication. A network error, invalid JSON/HTML/Markdown pricing data, or an invalid/empty required source fails the command and workflow, leaving the last published snapshot untouched. The file is written atomically only after validation. Historical successful source metadata is not rewritten to claim a failed fetch succeeded. SHA-256 records the fetched bytes; it does not authenticate upstream data or guarantee price accuracy.

## Agent coverage

Agent names are not tariffs. Muteki engines such as Pi, OMP, and OpenCode use their underlying serving provider and model prices; the engine name alone cannot select a rate. Cursor and Devin have their own official price namespaces because their listed usage prices and variants can differ from the underlying provider. Exact provider/model IDs and request billing dimensions remain necessary. Missing coverage stays unknown.

| Agent | Price lookup | Coverage boundary |
| --- | --- | --- |
| Cursor | `cursor/<model-id>` from Cursor's official table | Standard, Fast, and long-context variants are separate. Auto and unverified IDs remain unpriced. |
| Devin | `devin/swe-2-medium`, `devin/swe-2-high`, `devin/swe-2-max` | SWE-2 public list prices; account promotions, enterprise rates, and Fusion combinations require account data. |
| Codex, Claude Code, Grok | Exact provider/model entries such as `openai/…`, `anthropic/…`, `xai/…` | Public API reference rates; a subscription's actual charges cannot be inferred from the engine name. |
| Droid, Pi, OMP, OpenCode, ZCode | The actual serving provider and model in the catalogs | Custom endpoints, private aliases, and negotiated prices need an explicit mapping or local price override. |
| Kimi Code | Public Moonshot API prices when that API is used | Coding-plan IDs such as `k3` and `kimi-for-coding` are [subscription routes](https://www.kimi.com/en/help/kimi-code/models), not interchangeable USD tariffs. |

This is a price-source coverage guide, not a claim that every listed Agent is installed or every model is priced. The feed contains public pricing data only, with no local account, session, or private endpoint inventory.

## Updates and local use

GitHub Actions is scheduled daily at **03:17 UTC (11:17 Asia/Shanghai)** and supports **Run workflow** under Actions → Sync model prices. Scheduled jobs can be delayed or skipped by GitHub; prices can also lag their upstream sources. This feed is not real time and is not a billing authority. Check provider billing documentation for authoritative current prices.

Python 3.10+ and its standard library are sufficient:

```sh
python3 scripts/sync_prices.py
python3 scripts/sync_prices.py --check
# Optional destination; --check performs no fetch or writes.
python3 scripts/sync_prices.py --output /tmp/prices.json
```

Keys are sorted for deterministic ordering. Fetch and generation timestamps change on successful runs, so the daily snapshot is normally committed even when prices have not changed. The workflow runs only on the default branch, uses `contents: write` to commit only `prices.json` as `github-actions[bot]`, and has no pull-request trigger. Publication depends on Actions and repository write policies permitting that commit.

## 中文使用

直接读取上面的固定 `prices.json` 地址。LiteLLM 原始键和计价维度优先保留，models.dev 只补充缺失的“供应商/模型”键，Cursor 官方公开 token 价格使用 `cursor/<官方模型ID>` 独立键，不猜别名、不跨供应商混价。标准 token 单价为美元/token，models.dev、Cursor 和 Devin 的美元/百万 token 已除以一百万；`_source_cost` 保留原始单位。缺失、`null` 或未知价格不能当作免费，只有明确公布的数字零才是零。`_meta` 是元数据，`_price_provenance` 标记每条来源。

每天北京时间 11:17 计划同步，GitHub 调度和上游数据都可能延迟，不保证实时。任一必需来源失败就不发布，保留上一份成功数据。可在 Actions 中手动运行，也可执行上述 Python 命令。Cursor 是基础标价或额外用量估算，不含月订阅费、套餐 token 附加费和区域差异；必须区分标准、Fast 和长上下文版本，Composer 默认 Fast 不能按标准版估算。Auto/default 不提供固定单价。Devin 只发布 SWE-2 三个推理档位的官方公开标价，不包含订阅额度、促销和企业折扣，不调用认证模型接口。实际账单应以供应商为准。

## License and upstream attribution

Original synchronization code and documentation are [MIT licensed](LICENSE). LiteLLM and models.dev data retain their upstream licenses and notices, reproduced in [THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES).

- LiteLLM data is outside its enterprise directory and covered by the [root MIT license](https://github.com/BerriAI/litellm/blob/main/LICENSE), copyright © 2023 Berri AI.
- models.dev is covered by its [MIT license](https://github.com/anomalyco/models.dev/blob/dev/LICENSE), copyright © 2025 models.dev.
- Cursor entries extract factual numeric rates and model identifiers from [official public documentation](https://cursor.com/docs/models-and-pricing). Cursor and Devin documentation are not covered by the two upstream MIT licenses or this repository’s code license. No license to that documentation is claimed; documentation prose is not redistributed.

- Devin SWE-2 entries extract factual numeric list rates and public model identifiers from [official documentation](https://docs.devin.ai/desktop/models.md). No license to Devin documentation is claimed; documentation prose and account-specific data are not redistributed.

LiteLLM and models.dev are community-maintained snapshots; Cursor and Devin rates come from official public documentation. Source inclusion does not imply provider endorsement or a billing guarantee.
