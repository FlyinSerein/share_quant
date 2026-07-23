# 多因子边际贡献与经济属性分组验证

## 研究协议

- 训练期：2022-01-01 至 2024-12-31；样本外期：2025-01-01 至 20260707。
- 因子与方向来自冻结源配置 `walk_forward_champion.yaml`，未使用样本外收益调整因子或权重。
- D10-D1 为传统持有期毛收益；Top20 扣除单边 0.10% 换手成本后与 `000985.CSI` 比较。
- 边际贡献为“包含该因子的组合减不包含该因子的对应基准”，属于基准依赖的局部贡献，不具备 Shapley 可加性。

## 样本外组合对比

| 组合 | D10-D1年化 | Top20年化超额 | RankIC均值 | D10最大回撤 | Top20最大回撤 |
| --- | --- | --- | --- | --- | --- |
| Full11_Equal | 9.05% | 6.85% | 0.0094 | -4.14% | -17.21% |
| Fixed4_Equal | -12.29% | -3.13% | 0.0481 | -23.06% | -14.69% |
| Grouped8_Equal | 11.82% | 8.31% | 0.0008 | -3.04% | -17.93% |

## 1. 因子的正负边际贡献

- LOO 稳定正向 D10-D1：Debt_to_Equity、Gross_Margin、Main_Net_In、Revenue_Growth；稳定负向：Dividend_Yield、Turnover_20D、Volatility。
- LOO 稳定正向 Top20 超额：Holder_Concen、Momentum_60D、PE_TTM、Revenue_Growth、Turnover_20D；稳定负向：Debt_to_Equity、Dividend_Yield、Gross_Margin、Main_Net_In、ROE、Volatility。
- Add-back 稳定正向 D10-D1：Debt_to_Equity、Gross_Margin、Holder_Concen、Momentum_60D、ROE、Revenue_Growth、Turnover_20D；稳定负向：无。
- Add-back 稳定正向 Top20 超额：Debt_to_Equity、Gross_Margin、Holder_Concen、Momentum_60D、ROE、Revenue_Growth；稳定负向：Turnover_20D。
- 两个主要指标独立判断；一个因子在两个指标上结论冲突时不合并为单一评分。

| 实验 | 因子 | D10-D1边际 | D10稳定性 | Top20超额边际 | Top20稳定性 | RankIC边际 |
| --- | --- | --- | --- | --- | --- | --- |
| Add-back | Debt_to_Equity | 7.74% | stable_positive | 2.26% | stable_positive | 0.0018 |
| Add-back | Gross_Margin | 6.36% | stable_positive | 2.43% | stable_positive | 0.0007 |
| Add-back | Holder_Concen | 8.37% | stable_positive | 1.99% | stable_positive | -0.0035 |
| Add-back | Momentum_60D | 14.63% | stable_positive | 4.25% | stable_positive | -0.0182 |
| Add-back | ROE | 10.22% | stable_positive | 1.92% | stable_positive | -0.0007 |
| Add-back | Revenue_Growth | 18.45% | stable_positive | 6.06% | stable_positive | 0.0091 |
| Add-back | Turnover_20D | 2.15% | stable_positive | -0.09% | stable_negative | -0.0428 |
| LOO | Debt_to_Equity | 2.85% | stable_positive | -0.23% | stable_negative | 0.0011 |
| LOO | Dividend_Yield | -2.29% | stable_negative | -1.51% | stable_negative | 0.0059 |
| LOO | Gross_Margin | 0.63% | stable_positive | -0.09% | stable_negative | 0.0020 |
| LOO | Holder_Concen | -0.31% | mixed | 1.20% | stable_positive | 0.0001 |
| LOO | Main_Net_In | 1.68% | stable_positive | -1.20% | stable_negative | -0.0009 |
| LOO | Momentum_60D | 2.73% | mixed | 2.30% | stable_positive | -0.0128 |
| LOO | PE_TTM | 0.06% | mixed | 0.75% | stable_positive | 0.0045 |
| LOO | ROE | -0.51% | mixed | -0.54% | stable_negative | 0.0024 |
| LOO | Revenue_Growth | 7.26% | stable_positive | 3.52% | stable_positive | 0.0062 |
| LOO | Turnover_20D | -1.43% | stable_negative | 1.00% | stable_positive | -0.0248 |
| LOO | Volatility | -0.49% | stable_negative | -1.14% | stable_negative | 0.0188 |

## 2. 原始 11 因子为何优于固定 4 因子

- 原始 11 因子相对固定 4 因子的 D10-D1 年化差为 21.34%，Top20 年化超额差为 9.98%。
- 被筛除因子的 Add-back 与 LOO 结果表明，弱单因子 RankIC 不等于没有组合价值；部分因子通过截面排序和风格分散改善至少一个主要指标。
- 训练期 8 个经济组之间的平均绝对相关性为 0.0901。该结果用于验证分散程度，但不把相关性本身解释为收益因果。

## 3. 经济属性分组是否同时改善两个主要指标

- 相对原始 11 因子，分组组合的 D10-D1 变化为 2.76%，Top20 年化超额变化为 1.46%。
- 结论：两个主要指标均改善。

## 4. 跨样本外子区间稳定性

- 稳定标签要求全样本外贡献同号，且 2025H1、2025H2、2026YTD 中至少两个子区间同号。
- 分组组合相对原始 11 因子在两个主要指标上的子区间改善稳定。
- 完整逐月与分期结果见 `monthly_marginal_contribution.csv` 和 `period_marginal_contribution.csv`，分期仅用于归因，未用于修改规则。

## 5. 是否有充分证据替换原始 11 因子等权组合

**有充分证据替换原始 11 因子等权组合。**

该判断要求候选分组组合在全样本外同时改善 D10-D1 和 Top20 超额，并在多个预定义子区间保持方向稳定。允许最终结论为没有稳定改进方案，不根据本次样本外结果继续调参。
