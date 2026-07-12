# Factor Research

本项目只读调用相邻 `database/` 项目构建的 A 股研究数据库，负责单因子、中性化、因子诊断、多因子合成、分层回测和报告生成。

## 安装与配置

```powershell
cd factor_research
python -m pip install -e .
```

默认配置位于 `configs/default.yaml`：

```yaml
database_path: ../database/data/share_quant.duckdb
output_root: outputs
```

数据库连接始终使用只读模式，研究代码不得写入数据库项目的数据目录。

## 分阶段运行

```powershell
python scripts/run_factor_backtest.py
python scripts/run_factor_diagnostics.py
python scripts/run_multifactor_layered_backtest.py
```

原有日期、基准、交易成本和其他命令参数保持不变，可使用 `--help` 查看。

## 输出

- `outputs/single_factor/`：原始单因子 Top20% 回测及报告。
- `outputs/neutralized/`：行业和市值中性化结果及比较报告。
- `outputs/diagnostics/`：IC、分组收益、暴露和多空诊断。
- `outputs/multifactor/`：多因子合成、分层回测和报告。

各阶段相互独立，运行一个阶段不会清理其他阶段的历史成果。已有 ZIP 交付文件保存在对应阶段的 `exports/` 中。

## 测试

```powershell
python -m pytest
```
