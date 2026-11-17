# Mathematics 教案规范

本目录及其子目录中的教学材料应遵守以下规则。

## 图表语言

- 所有作图元素一律使用英文：标题、坐标轴标签、图例、刻度文字、注释、悬浮提示和图内文字。
- 不要在 Matplotlib、Plotly、Wolfram Language 图形或任何导出的图像中使用中文，以避免字体缺失或乱码。
- 正文、Markdown 解释、Notebook 章节标题可以使用中文。

## 数学排版

- Jupyter Notebook 的展示公式使用 MathJax 块公式（两个美元符号包围公式）。
- 数学符号使用标准 LaTeX 命令，例如 \\rho、\\beta、\\varepsilon、\\Delta、\\gamma、\\theta、\\psi。
- 不要在公式块中写字面量 \\n；若需要换行，使用真实换行或将公式拆分成多个公式块。

## 可运行性

- Jupyter 教案代码默认使用项目的 quant Python 环境。
- 新增或修改教案后，验证 Notebook JSON 可解析，并执行相关代码单元。
- Mathematica .nb 文件须保持 Wolfram Notebook 格式可读。
