# 唯一命令入口。所有文档里的命令都必须能在这跑通（G3 命令实跑门）。
# 解释器固定：系统 python 没有 langgraph。
PY ?= /Users/tj/.workbuddy/binaries/python/envs/default/bin/python

.PHONY: help gate test demo init sem regress clean

help:
	@echo "make gate      # 5 道门禁（提交前必跑）"
	@echo "make test      # 两套自检（32 + 77）"
	@echo "make demo      # 10 题演示"
	@echo "make init      # 重建 artifacts/v2_dialogue.db"
	@echo "make sem       # 语义层评估器 ev13-18 + 反向"
	@echo "make regress   # 7 题字节级回归"
	@echo "make clean     # 清临时库（保留源码）"

gate:
	$(PY) ci/gate_all.py

test:
	$(PY) selftest.py
	$(PY) selftest_v2.py

demo:
	$(PY) v2_dialogue.py

init:
	$(PY) -c "from pathlib import Path; import v2_dialogue as V; V.prepare(Path('artifacts/v2_dialogue.db'))"

sem:
	$(PY) eval/eval_semantic.py

regress:
	$(PY) eval/regress_demo.py

# 以下目标在对应阶段实现后才启用（现在跑会失败，故注释保留，避免「门是画的」）
# replay:      $(PY) eval/replay.py --all
# eval-llm:    $(PY) eval/runner.py --all --llm
# mcp:         $(PY) -m mcp_server.policy_catalog
# record:      $(PY) eval/record.py --case $(CASE)

clean:
	rm -f artifacts/v2_dialogue.db-journal artifacts/*.db-journal
	@echo "已清临时文件；要重建库用 make init"
