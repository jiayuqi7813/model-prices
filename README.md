# Model prices

A daily, public snapshot of model prices from [LiteLLM](https://github.com/BerriAI/litellm) and [models.dev](https://models.dev). No API key, account, or paid inference request is needed.

Stable raw feed: [prices.json](https://raw.githubusercontent.com/jiayuqi7813/model-prices/main/prices.json)

```sh
curl -fsSL https://raw.githubusercontent.com/jiayuqi7813/model-prices/main/prices.json
```

## Data contract

`prices.json` is a flat map compatible with LiteLLM token-price fields. `_meta` is reserved metadata, not a model. Ignore keys beginning with `_` when enumerating models. `_meta.schema_version` is `1`; `_meta.generated_at` is UTC. Each required source records its URL, fetch time, SHA-256 of the fetched bytes, source entry count, and `status: "ok"`. These timestamps describe our fetch, not the upstream price update time.

- **LiteLLM first:** exact model keys and all original fields are preserved, including cache, priority/fast, long-context, audio, and other billing dimensions. Its `sample_spec` documentation example is excluded because its zeros are not actual model prices.
- **models.dev additions:** only missing `provider_id/model_id` keys are added. Existing keys are never merged or overwritten, even when their prices are incomplete. This avoids guessing provider equivalence. For example, `openrouter/openai/gpt-5` and `openai/gpt-5` represent different serving providers. Model IDs already containing `/` still receive the provider prefix. No fuzzy aliases or canonical-model aliases are synthesized.
- **Units:** mapped token-price fields are USD per token. models.dev publishes USD per million tokens, so mapped values are divided by 1,000,000. `_source_cost` retains its original cost object and units, including dimensions not mapped to LiteLLM fields. LiteLLM's non-token price fields retain their original units indicated by their names.
- **Unknown values:** missing, `null`, malformed, negative, or non-finite prices are never converted to zero. Original LiteLLM fields are retained, including incomplete records. Invalid models.dev values are not mapped. Published numeric zeros remain zero. Consumers must validate the fields they use.
- **Computability:** `_price_status: "computable"` means both base input and output token rates are finite, nonnegative numbers, with booleans rejected. Otherwise it is `"unknown"`. This is a base token estimate, not a guarantee that every charge is covered. Consumers still need to select applicable cache, context, service-tier, modality, and other billing dimensions.
- **Provenance:** every model has `_price_provenance` with its source and source key; models.dev records also have the provider ID and original unit. No record mixes prices from different sources or providers.

Both sources are required for each publication. A network error, invalid JSON, or an invalid/empty required source fails the command and workflow, leaving the last published snapshot untouched. The file is written atomically only after validation. Historical successful source metadata is not rewritten to claim a failed fetch succeeded. SHA-256 records the fetched bytes; it does not authenticate upstream data or guarantee price accuracy.

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

直接读取上面的固定 `prices.json` 地址。LiteLLM 原始键和计价维度优先保留，models.dev 只补充缺失的“供应商/模型”键，不猜别名、不跨供应商混价。标准 token 单价为美元/token，models.dev 的美元/百万 token 已除以一百万；`_source_cost` 保留原始单位。缺失、`null` 或未知价格不能当作免费，只有明确公布的数字零才是零。`_meta` 是元数据，`_price_provenance` 标记每条来源。

每天北京时间 11:17 计划同步，GitHub 调度和上游数据都可能延迟，不保证实时。任一必需来源失败就不发布，保留上一份成功数据。可在 Actions 中手动运行，也可执行上述 Python 命令。实际账单应以供应商为准。

## License and upstream attribution

Original synchronization code and documentation are [MIT licensed](LICENSE). The redistributed upstream data retains its upstream licenses and notices, reproduced in [THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES).

- LiteLLM data is outside its enterprise directory and covered by the [root MIT license](https://github.com/BerriAI/litellm/blob/main/LICENSE), copyright © 2023 Berri AI.
- models.dev is covered by its [MIT license](https://github.com/anomalyco/models.dev/blob/dev/LICENSE), copyright © 2025 models.dev.

These are community-maintained source snapshots. Source inclusion does not imply provider endorsement or independently verified prices.
