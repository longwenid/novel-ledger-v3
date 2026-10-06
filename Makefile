.PHONY: test test-full check audit clean help

PYTHON ?= python

help:
	@echo "novel-ledger-v3 维护指令:"
	@echo "  make test      - 默认测试闸（跳过 slow 规模仿真，约几分钟）"
	@echo "  make test-full - 全量测试套件（含千章规模回归，十几分钟）"
	@echo "  make check     - 运行仓库布局与不变量检查"
	@echo "  make audit     - CLI 冒烟（--help 可解析）"
	@echo "  make clean     - 清理 __pycache__ 与临时缓存"
	@echo "  变异/压力测试工程留在 v1 仓库根的 .dev/（v3 未携带）"

test:
	$(PYTHON) -m pytest

test-full:
	$(PYTHON) -m pytest -m ""

check:
	$(PYTHON) -m pytest scripts/tests/test_repo_layout.py -q

audit: check
	$(PYTHON) scripts/novel_ledger.py --help > /dev/null

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type d -name ".pytest_cache" -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
