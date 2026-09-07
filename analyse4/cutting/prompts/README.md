# Prompt 版本

每个子目录是一个可复现的 Prompt 版本，至少包含：

- `system.txt`：系统指令；
- `user_template.txt`：用户模板，必须保留 `{{units_json}}` 占位符；
- `metadata.json`：版本说明、状态和迭代备注。

新建版本时复制现有版本目录并改名，例如：

```powershell
Copy-Item prompts/v001_fact_single prompts/v002_strict_fact -Recurse
```

然后只修改新目录中的 Prompt 文件，用新的 `--prompt-version` 和 `--run-id` 运行。每次 run 的 `run_manifest.json` 会保存实际使用的 Prompt 全文和 SHA-256，避免后续修改文件后无法追溯。
