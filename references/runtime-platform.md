# Windows / Git Bash 运行平台

文档命令默认写 `python3`。Windows 上先探测 Python 3.9+，本会话后续命令用 `$NL_PY`
替代 `python3`。`python3` 可能是 Microsoft Store 占位程序，`python` 也可能指向旧版。

```bash
NL_PY=""
for cand in "py -3" python python3; do
  if $cand -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
    NL_PY="$cand"; break
  fi
done
$NL_PY "$SKILL_ROOT/scripts/novel_ledger.py" --version
```

`NL_PY` 为空说明没有可用解释器；变量不加引号，因为 `py -3` 是两个词。Git Bash 命令路径
统一用正斜杠；中转文件不要放 `/tmp`，Windows Python 看不到该映射。跨命令用绝对路径或
显式 `cd`，不要假设 shell 工作目录会保留。`.dev/plugin-hooks/session-start` 也会探测解释器。

## 中文实参与输出编码

CLI 自身的 stdout/stderr 已强制 UTF-8（进程内 reconfigure），JSON 回执里的中文路径/
引文/摘句不会再被控制台码页打坏。但**中文实参**坏码发生在 shell 传参层，进程内救不了：
pwsh 按系统码页收参，inline 中文（`--replacement`、ack 引文、`--reason` 长句）可能逐字节
损坏（实测 pwsh 会话 inline 中文引文坏码，改走文件后一次过）。规约：

- 凡带中文内容的参数一律走**文件中转**：`rework-patch --patch-file <json>`、
  `ack-read --quotes-file <json 列表文件>`、组装申报写 staging JSON 后 `chapter submit --output`。
- 宿主侧 console 也设 UTF-8（pwsh：`[Console]::OutputEncoding=[Text.Encoding]::UTF8`），
  与 CLI 输出面一致。
- 调度卡 `cli_invocation` 字段是运行中实例的规范化调用行（绝对 CLI 路径 + 项目路径），
  宿主逐字复制，不要自行解析 SKILL_ROOT——跨宿主 junction/安装副本解析会漂移
  （实测同一会话中途在 `.dsh` 与 `.zcode` 路径间切换）。
