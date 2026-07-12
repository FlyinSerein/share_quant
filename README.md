# share_quant workspace

这是一个包含两个独立 Python 项目的量化研究工作区：

- `database/`：构建和维护本地 A 股 DuckDB/Parquet 研究数据库。
- `factor_research/`：只读调用数据库，完成因子、诊断、回测和报告。

两个项目使用同一个 Git 仓库，但拥有各自的包、配置、依赖、测试和文档。

## 数据库

```powershell
cd database
python -m pytest
python -m share_quant.cli validate
```

## 因子研究

```powershell
cd factor_research
python -m pip install -e .
python scripts/run_factor_backtest.py
python scripts/run_factor_diagnostics.py
python scripts/run_multifactor_layered_backtest.py
```
