(* AA/CENX statistical-arbitrage lab.
   Read-only MySQL.  Fixed beta is specified by the user; spread statistics are trailing. *)

ClearAll[AACENXOpenReadOnly, AACENXPairRegistry, AACENXDailyRead, AACENXEndpointDetrend,
  AACENXEndpointBeta, AACENXFixedSpread, AACENXPairView, AACENXDailyMovingAverageLab,
  AACENXSineSpreadLab, AACENXHalfLifeSineLab, AACENXOneCycleHalfLifeDemo, AACENXLambdaHatFiveSteps, AACENXOLSGeometryDemo, AACENXSlowingSineLambdaDemo,
  AACENXAcrossEvents, AACENXRollingLambda, AACENXInverseRollingHalfLife, AACENXDensityLab];

AACENXOpenReadOnly[] := Module[{connection},
  Quiet[Needs["DatabaseLink`"]];
  connection = Check[
    DatabaseLink`OpenSQLConnection[
      DatabaseLink`JDBC["MySQL(Connector/J)", "localhost/quant_data"],
      "Username" -> "quant", "Password" -> "1q2w3e", "ReadOnly" -> True
    ],
    Failure["DatabaseConnection", <|
      "Message" -> "无法连接 MySQL。请确认 DatabaseLink 与 MySQL Connector/J 可用。"|>]
  ]; connection
];

AACENXPairRegistry[] := Module[{connection, rows},
  connection = AACENXOpenReadOnly[]; If[Head[connection] === Failure, Return[connection]];
  rows = Check[
    DatabaseLink`SQLExecute[connection,
      "SELECT pair_name, ticker_a, ticker_b FROM metapairs " <>
      "WHERE COALESCE(is_active,1)=1 ORDER BY pair_name", "GetAsStrings" -> True],
    DatabaseLink`CloseSQLConnection[connection];
    Return[Failure["RegistryRead", <|"Message" -> "读取 quant_data.metapairs 失败。"|>]]
  ];
  DatabaseLink`CloseSQLConnection[connection];
  Association @ Map[
    Function[row, ToString[row[[1]]] -> {ToUpperCase@ToString[row[[2]]], ToUpperCase@ToString[row[[3]]]}],
    Select[rows, Length[#] >= 3 && StringMatchQ[ToString[#[[2]]], LetterCharacter ..] &&
      StringMatchQ[ToString[#[[3]]], LetterCharacter ..] &]
  ]
];

AACENXDailyRead[tickerY_String, tickerX_String] := Module[
  {y, x, connection, rows, number, normalized, daily, source},
  y = ToUpperCase@StringTrim[tickerY]; x = ToUpperCase@StringTrim[tickerX];
  If[!StringMatchQ[y, LetterCharacter ..] || !StringMatchQ[x, LetterCharacter ..] || y === x,
    Return[Failure["InvalidPair", <|"Message" -> "ticker 必须是两个不同的英文字母代码。"|>]]
  ];
  connection = AACENXOpenReadOnly[]; If[Head[connection] === Failure, Return[connection]];
  (* Build daily closes from the full 5-minute history.  Each ticker gets its
     own final bar of the UTC trading date, then the two daily series are
     joined on the date.  This deliberately does not require matching final
     intraday timestamps, which can differ between two legs. *)
  rows = Quiet@Check[
    DatabaseLink`SQLExecute[connection,
      "SELECT yd.trade_date, y.price, x.price FROM " <>
      "(SELECT DATE(time_utc) AS trade_date, MAX(time_utc) AS close_time " <>
      " FROM us_market_prices WHERE ticker='" <> y <> "' AND time_utc >= '2022-01-01' " <>
      " AND price IS NOT NULL GROUP BY DATE(time_utc)) yd " <>
      "JOIN us_market_prices y ON y.ticker='" <> y <> "' AND y.time_utc=yd.close_time " <>
      "JOIN (SELECT DATE(time_utc) AS trade_date, MAX(time_utc) AS close_time " <>
      " FROM us_market_prices WHERE ticker='" <> x <> "' AND time_utc >= '2022-01-01' " <>
      " AND price IS NOT NULL GROUP BY DATE(time_utc)) xd ON xd.trade_date=yd.trade_date " <>
      "JOIN us_market_prices x ON x.ticker='" <> x <> "' AND x.time_utc=xd.close_time " <>
      "ORDER BY yd.trade_date",
      "GetAsStrings" -> True], $Failed];
  source = "us_market_prices（自 2022 起，每腿每日最后一个 5 分钟 bar）";
  If[rows === $Failed,
    DatabaseLink`CloseSQLConnection[connection];
    Return[Failure["DatabaseRead", <|"Message" ->
      "无法从 us_market_prices 构造 2022 年起的共同日线。"|>]]
  ];
  DatabaseLink`CloseSQLConnection[connection];
  number[value_] := Quiet@Check[N@ToExpression[ToString[value], InputForm], Indeterminate];
  normalized = ({StringTake[ToString[#[[1]]], 10], number[#[[2]]], number[#[[3]]]} &) /@ rows;
  (* 查询已经按日期输出一行；GatherBy 留作防御性去重。 *)
  daily = Last /@ GatherBy[Select[normalized, NumericQ[#[[2]]] && NumericQ[#[[3]]] &], First];
  If[Length[daily] < 100,
    Failure["InsufficientData", <|"Message" ->
      "2022 年起的共同日线不足；请确认两只 ticker 都在 us_market_prices 中有历史。"|>],
    <|"Dates" -> (DateObject /@ daily[[All, 1]]),
      "Y" -> y, "X" -> x, "PricesY" -> daily[[All, 2]], "PricesX" -> daily[[All, 3]],
      "Source" -> source|>
  ]
];

AACENXFixedSpread[aa_List, cenx_List, beta_?NumericQ] := Module[
  {aaNorm, cenxNorm},
  aaNorm = aa/First[aa];
  cenxNorm = cenx/First[cenx];
  <|"AANormalized" -> aaNorm, "CENXNormalized" -> cenxNorm,
    "Spread" -> (aaNorm - beta cenxNorm)|>
];

AACENXEndpointDetrend[values_List] := Module[{n = Length[values], line},
  line = First[values] + (Last[values] - First[values]) Range[0, n - 1]/(n - 1);
  <|"Line" -> line, "Residual" -> (values - line)|>
];

AACENXEndpointBeta[data_Association] := Module[{aa, cenx, y, x, dy, dx, beta},
  {aa, cenx} = Lookup[data, {"PricesY", "PricesX"}];
  y = aa/First[aa]; x = cenx/First[cenx];
  dy = AACENXEndpointDetrend[y]["Residual"];
  dx = AACENXEndpointDetrend[x]["Residual"];
  beta = Dot[dx, dy]/Dot[dx, dx];
  <|"Beta" -> beta, "Residual" -> (dy - beta dx),
    "RMS" -> Sqrt[Mean[(dy - beta dx)^2]]|>
];

AACENXPairView[data_Association, meanWindow_Integer, beta_?NumericQ] := Module[
  {dates, yName, xName, aa, cenx, fixed, spread, endpoint, mean, stdev, statisticDates, topPlot, spreadPlot},
  {dates, yName, xName, aa, cenx} = Lookup[data, {"Dates", "Y", "X", "PricesY", "PricesX"}];
  fixed = AACENXFixedSpread[aa, cenx, beta];
  spread = fixed["Spread"];
  endpoint = AACENXEndpointDetrend[spread]["Line"];
  If[Length[spread] < meanWindow,
    Return[Style["均线窗口大于可用 spread 长度。", Darker@Red]]
  ];
  mean = MovingAverage[spread, meanWindow];
  stdev = StandardDeviation /@ Partition[spread, meanWindow, 1];
  statisticDates = dates[[meanWindow ;;]];
  topPlot = DateListPlot[
    {Transpose[{dates, fixed["AANormalized"]}], Transpose[{dates, fixed["CENXNormalized"]}]},
    Joined -> True, PlotStyle -> {Directive[RGBColor[.12, .27, .51], Thick], Directive[RGBColor[.95, .40, 0], Thick]},
    PlotLegends -> Placed[{yName <> " (normalised)", xName <> " (normalised)"}, Above],
    Frame -> True, FrameLabel -> {"date", "start = 1"}, GridLines -> {None, Automatic},
    GridLinesStyle -> Directive[GrayLevel[.87]], PlotRange -> All, ImageSize -> 1050, AspectRatio -> .25,
    PlotLabel -> Style[yName <> "/" <> xName <> "：归一化后的两条腿", 15, Bold]
  ];
  spreadPlot = DateListPlot[
    {Transpose[{dates, spread}], Transpose[{dates, endpoint}], Transpose[{statisticDates, mean}],
      Transpose[{statisticDates, mean + 2 stdev}], Transpose[{statisticDates, mean - 2 stdev}]},
    Joined -> True,
    PlotStyle -> {Directive[RGBColor[.25, .65, 1.], Thick], Directive[LightGray, Dashed, Thick], Directive[White, Thick],
      Directive[Red, Dashed], Directive[Red, Dashed]},
    PlotLegends -> Placed[Style[#, White] & /@ {"spread = " <> yName <> " - beta " <> xName, "endpoint trend line",
       ToString[meanWindow] <> "d trailing mean", "+/- 2 trailing sigma"}, Above],
    Background -> RGBColor[.055, .07, .10], Frame -> True, FrameStyle -> White,
    FrameLabel -> {"date", "normalised spread"}, LabelStyle -> White, GridLines -> {None, Automatic},
    GridLinesStyle -> Directive[GrayLevel[.32]], PlotRange -> All, ImageSize -> 1050, AspectRatio -> .31,
    PlotLabel -> Style["fixed beta = " <> ToString@NumberForm[beta, {5, 3}] <>
      " ; every band is trailing", 15, Bold, White]
  ];
  Column[{topPlot, spreadPlot}, Spacings -> 1.1]
];

AACENXDailyMovingAverageLab[] := DynamicModule[
  {registry = <||>, pairName = "aa_cenx", tickerY = "SLV", tickerX = "SIVR", data = None,
   meanWindow = 20, beta = 1.,
   status = "可直接输入 SLV / SIVR；读取 us_market_prices 自 2022 年起的日线。"},
  Column[{
    Style["metapairs · 统计套利 spread 实验", 24, Bold],
    Style["两条腿先按共同首日归一化。spread[t] = Y[t] - beta X[t]，其中 beta 是你指定并固定的数。均线与 sigma 只使用截至 t 日的 spread；没有未来函数。", 12],
    Row[{
      Button["从 metapairs 读取 pair 列表",
        status = "正在读取 metapairs…"; registry = AACENXPairRegistry[];
        If[Head[registry] === Failure, status = registry["Message"],
          If[!KeyExistsQ[registry, pairName], pairName = First[Keys[registry]]];
          status = Row[{"已载入 ", Length[registry], " 个 active pair。"}]
        ]
      ], Spacer[8],
      Dynamic[If[AssociationQ[registry] && Length[registry] > 0,
        PopupMenu[Dynamic[pairName], Keys[registry], FieldSize -> 18], "先读取 pair 列表"]],
      Spacer[8], Button["加载所选 pair 的共同日线",
        If[AssociationQ[registry] && KeyExistsQ[registry, pairName],
          status = "正在读取…"; data = AACENXDailyRead @@ registry[pairName];
          If[Head[data] === Failure,
            status = data["Message"],
            With[{fit = AACENXEndpointBeta[data]}, beta = fit["Beta"];
            status = Row[{pairName, "：已载入 ", Length[data["Dates"]], " 个共同交易日（", data["Source"], "）；",
              "默认 beta = ", NumberForm[beta, {6, 4}], "（端点去趋势残差方差最小）：",
              DateString[First[data["Dates"]], {"Year", "-", "Month", "-", "Day"}], " 至 ",
              DateString[Last[data["Dates"]], {"Year", "-", "Month", "-", "Day"}]}]]
          ],
          status = "请先从 metapairs 读取 pair 列表。"
        ]
      ], Spacer[12], "直接输入：Y ",
      InputField[Dynamic[tickerY], String, FieldSize -> 6], " X ",
      InputField[Dynamic[tickerX], String, FieldSize -> 6], Spacer[6],
      Button["加载输入的共同日线",
        status = "正在读取…"; data = AACENXDailyRead[tickerY, tickerX];
        If[Head[data] === Failure, status = data["Message"],
          With[{fit = AACENXEndpointBeta[data]}, beta = fit["Beta"];
          status = Row[{data["Y"], "/", data["X"], "：已载入 ", Length[data["Dates"]],
            " 个共同交易日（", data["Source"], "）；默认 beta = ", NumberForm[beta, {6, 4}],
            "（端点去趋势残差方差最小）：",
            DateString[First[data["Dates"]], {"Year", "-", "Month", "-", "Day"}], " 至 ",
            DateString[Last[data["Dates"]], {"Year", "-", "Month", "-", "Day"}]}]]
        ]
      ], Spacer[12],
      "spread mean window: ", Slider[Dynamic[meanWindow], {2, 252, 1}, ImageSize -> 220], Dynamic[meanWindow],
      Spacer[14], "fixed beta: ", InputField[Dynamic[beta], Number, FieldSize -> 6],
      Spacer[8], Button["fit endpoint-detrended global beta",
        If[AssociationQ[data],
          With[{fit = AACENXEndpointBeta[data]}, beta = fit["Beta"];
            status = Row[{"全样本研究 beta = ", NumberForm[fit["Beta"], {6, 4}],
              "；端点趋势残差 RMS = ", NumberForm[fit["RMS"], {5, 4}],
              "。请在独立未来样本中检验此固定 beta。"}]
          ]
        ]
      ]
    }],
    Dynamic[Style[status, 11, Darker@Blue]],
    Dynamic[If[AssociationQ[data] && NumericQ[beta], AACENXPairView[data, meanWindow, beta],
      Panel["选择一个 metapairs pair，并加载其共同日线。"]],
      TrackedSymbols :> {data, meanWindow, beta}, SynchronousUpdating -> False]
  }, Spacings -> 1.1, BaseStyle -> {FontFamily -> "Noto Sans CJK SC"}]
];

(* Retired research prototype: local kx+b clustering was not retained as a
   density measure.  The executable blackboard now uses AACENXAcrossEvents. *)
(*
AACENXStandardizeColumn[values_List] := Module[{scale = StandardDeviation[values]},
  If[!NumericQ[scale] || PossibleZeroQ[scale], ConstantArray[0., Length[values]],
    N[(values - Mean[values])/scale]]
];

AACENXRollingMeanClusters[data_Association, meanWindow_Integer, beta_?NumericQ,
   fitWindow_Integer, requestedClusters_Integer] := Module[
  {dates, pricesY, pricesX, spread, mean, meanDates, usable, x, localFits,
   slopes, intercepts, rSquared, fitted, features, scaledFeatures, clusterCount,
   labels, featureDates, clusterColours, meanPlot, featurePlot, segmentRows,
   segmentTable, runStarts, runEnds, runLabels, row},
  {dates, pricesY, pricesX} = Lookup[data, {"Dates", "PricesY", "PricesX"}];
  spread = AACENXFixedSpread[pricesY, pricesX, beta]["Spread"];
  If[Length[spread] < meanWindow + fitWindow - 1,
    Return[Style["数据不足：需要至少 mean window + local fit window - 1 个共同交易日。", Darker@Red]]
  ];
  mean = MovingAverage[spread, meanWindow];
  meanDates = dates[[meanWindow ;;]];
  usable = Range[fitWindow, Length[mean]];
  x = Range[Length[mean]];
  (* Every local fit ends at i and only uses mean[[i-fitWindow+1 ;; i]].
     Therefore k, b and R^2 for that row are known at that day's close. *)
  localFits = Table[
    Module[{xs = x[[i - fitWindow + 1 ;; i]], ys = mean[[i - fitWindow + 1 ;; i]], model, prediction, residual},
      model = Fit[Transpose[{xs, ys}], {1, u}, u];
      prediction = model /. u -> xs;
      residual = ys - prediction;
      <|"Slope" -> N@Coefficient[model, u, 1], "Intercept" -> N@(model /. u -> 0),
        "FittedNow" -> N[model /. u -> i],
        "RSquared" -> If[PossibleZeroQ[Total[(ys - Mean[ys])^2]], 1.,
          N[1 - Total[residual^2]/Total[(ys - Mean[ys])^2]]]|>
    ], {i, usable}];
  slopes = Lookup[localFits, "Slope"];
  intercepts = Lookup[localFits, "Intercept"];
  fitted = Lookup[localFits, "FittedNow"];
  rSquared = Lookup[localFits, "RSquared"];
  (* Standardising means slope and level have equal influence on K-means.
     The original k and b remain available for interpretation and plotting. *)
  features = Transpose[{slopes, intercepts}];
  scaledFeatures = Transpose[AACENXStandardizeColumn /@ Transpose[features]];
  clusterCount = Max[2, Min[requestedClusters, Length[scaledFeatures]]];
  (* ClusteringComponents returns a hierarchy path (for example {2,4}) in
     current Wolfram versions.  The final component is the requested K-means
     label; use it so Pick and consecutive-run detection stay one-dimensional. *)
  labels = Last /@ ClusteringComponents[scaledFeatures, clusterCount, Method -> "KMeans"];
  featureDates = meanDates[[fitWindow ;;]];
  clusterColours = ColorData["BrightBands"] /@ Rescale[Range[clusterCount]];
  meanPlot = Show[
    DateListPlot[Transpose[{meanDates, mean}], Joined -> True,
      PlotStyle -> Directive[RGBColor[.25, .65, 1.], Thick],
      PlotLegends -> Placed[{ToString[meanWindow] <> "d trailing spread mean"}, Above]],
    DateListPlot[Table[Transpose[{Pick[featureDates, labels, c], Pick[fitted, labels, c]}], {c, clusterCount}],
      Joined -> False, PlotStyle -> (Directive[#, PointSize[.012]] & /@ clusterColours),
      PlotLegends -> Placed[Table["cluster " <> ToString[c] <> " local kx+b", {c, clusterCount}], Below]],
    Frame -> True, FrameLabel -> {"date", "normalised spread mean"}, GridLines -> {None, Automatic},
    GridLinesStyle -> Directive[GrayLevel[.87]], ImageSize -> 1050, AspectRatio -> .31,
    PlotLabel -> Style["每个彩点是截至该日的局部线性拟合值", 15, Bold]
  ];
  featurePlot = ListPlot[Table[Pick[features, labels, c], {c, clusterCount}],
    PlotStyle -> (Directive[#, PointSize[.015]] & /@ clusterColours),
    PlotLegends -> Placed[Table["cluster " <> ToString[c], {c, clusterCount}], Right],
    Frame -> True, FrameLabel -> {"local slope k", "local intercept b"}, GridLines -> Automatic,
    ImageSize -> 650, AspectRatio -> .65, PlotLabel -> Style["局部 k、b 特征空间（标准化后聚类）", 14, Bold]
  ];
  runStarts = Join[{1}, 1 + Flatten@Position[Differences[labels], _?(# != 0 &)]];
  runEnds = Join[Rest[runStarts] - 1, {Length[labels]}];
  runLabels = labels[[runStarts]];
  segmentRows = Table[
    row = Range[runStarts[[j]], runEnds[[j]]];
    <|"cluster" -> runLabels[[j]], "start" -> DateString[featureDates[[First[row]]], {"Year", "-", "Month", "-", "Day"}],
      "end" -> DateString[featureDates[[Last[row]]], {"Year", "-", "Month", "-", "Day"}],
      "days" -> Length[row], "mean k" -> Mean[slopes[[row]]], "mean b" -> Mean[intercepts[[row]]],
      "mean R2" -> Mean[rSquared[[row]]]|>, {j, Length[runStarts]}];
  segmentTable = Dataset[segmentRows];
  Column[{meanPlot, featurePlot,
    Style["连续段摘要：同一 cluster 可在不相邻日期重复出现；短段不是自动交易信号。", 12, Bold], segmentTable},
    Spacings -> 1.1]
];

AACENXRollingMeanClusterLab[] := DynamicModule[
  {tickerY = "SBLK", tickerX = "BHP", data = None, meanWindow = 20, beta = 1.,
   fitWindow = 20, clusterCount = 4, status = "输入 pair 后加载共同日线。"},
  Column[{
    Style["rolling spread mean 的局部线性聚类", 24, Bold],
    Style["先构造固定 beta 的 trailing spread mean。每个交易日只以最近 L 个已经完成的 mean 点拟合 k x + b，随后按 (k,b) 的标准化特征做 K-means。", 12],
    Row[{"Y ", InputField[Dynamic[tickerY], String, FieldSize -> 6], " X ",
      InputField[Dynamic[tickerX], String, FieldSize -> 6], Spacer[8],
      Button["加载共同日线", data = AACENXDailyRead[tickerY, tickerX];
        status = If[Head[data] === Failure, data["Message"],
          With[{fit = AACENXEndpointBeta[data]}, beta = fit["Beta"];
            Row[{data["Y"], "/", data["X"], "：", Length[data["Dates"]], " 个共同交易日（", data["Source"], "）；",
              "默认 beta = ", NumberForm[beta, {6, 4}], "（端点去趋势残差方差最小）"}]]]],
      Spacer[14], "fixed beta ", InputField[Dynamic[beta], Number, FieldSize -> 6],
      Spacer[12], "mean window ", Slider[Dynamic[meanWindow], {2, 60, 1}, ImageSize -> 120], Dynamic[meanWindow],
      Spacer[12], "local fit L ", Slider[Dynamic[fitWindow], {3, 60, 1}, ImageSize -> 120], Dynamic[fitWindow],
      Spacer[12], "clusters ", SetterBar[Dynamic[clusterCount], Range[2, 6]]}],
    Dynamic[Style[status, 11, Darker@Blue]],
    Style["无未来函数边界：每一个局部 k、b 的窗口都在当天结束；但 K-means 若用整段历史求中心，仅可作为研究分段。实盘需在训练期拟合并冻结中心，再为后续日期分配最近中心。", 12, Darker@Red],
    Dynamic[If[AssociationQ[data] && NumericQ[beta],
      AACENXRollingMeanClusters[data, meanWindow, beta, fitWindow, clusterCount],
      Panel["加载 pair 后显示局部 kx+b 聚类。"]],
      TrackedSymbols :> {data, meanWindow, beta, fitWindow, clusterCount}, SynchronousUpdating -> False]
  }, Spacings -> 1.1, BaseStyle -> {FontFamily -> "Noto Sans CJK SC"}]
];

*)

AACENXAcrossEvents[data_Association, meanWindow_Integer, beta_?NumericQ,
   excursionThreshold_?NumericQ] := Module[
  {dates, pricesY, pricesX, spread, mean, sigma, z, statisticDates, nonZero,
   candidates, separatingExcursionQ, across, life, density},
  {dates, pricesY, pricesX} = Lookup[data, {"Dates", "PricesY", "PricesX"}];
  spread = AACENXFixedSpread[pricesY, pricesX, beta]["Spread"];
  If[Length[spread] < meanWindow + 3,
    Return[Failure["InsufficientData", <|"Message" -> "数据不足，无法计算 rolling z-score 与 across 事件。"|>]]
  ];
  mean = MovingAverage[spread, meanWindow];
  sigma = StandardDeviation /@ Partition[spread, meanWindow, 1];
  z = MapThread[If[PossibleZeroQ[#2], Indeterminate, (#1 - #3)/#2] &, {spread[[meanWindow ;;]], sigma, mean}];
  statisticDates = dates[[meanWindow ;;]];
  (* Ignore exact zero plateaus, then retain the first non-zero observation on
     the new side whenever the sign changes. *)
  nonZero = Select[Range[Length[z]], NumericQ[z[[#]]] && !PossibleZeroQ[z[[#]]] &];
  candidates = If[Length[nonZero] < 2, {},
    Last /@ Select[Partition[nonZero, 2, 1], Sign[z[[#[[1]]]]] != Sign[z[[#[[2]]]]] &]
  ];
  (* A candidate is an across event only when the intervals to its immediate
     neighbouring zero crossings both contain a meaningful excursion. *)
  separatingExcursionQ[a_, b_] := Max[Abs[z[[a ;; b]]]] > excursionThreshold;
  across = If[Length[candidates] < 3, {},
    Pick[candidates[[2 ;; -2]],
      Table[separatingExcursionQ[candidates[[j - 1]], candidates[[j]]] &&
        separatingExcursionQ[candidates[[j]], candidates[[j + 1]]],
        {j, 2, Length[candidates] - 1}]]
  ];
  life = Differences[across];
  density = N[1/life];
  <|"Dates" -> statisticDates, "ZScore" -> z, "Candidates" -> candidates,
    "Across" -> across, "Life" -> life, "Density" -> density|>
];

AACENXRollingLambda[spread_List, lookback_Integer] := Module[
  {n = Length[spread], result, fitWindow, lagged, changes, model, lambda},
  result = ConstantArray[Indeterminate, n];
  Do[
    fitWindow = spread[[i - lookback + 1 ;; i]];
    lagged = Most[fitWindow]; changes = Differences[fitWindow];
    model = Fit[Transpose[{lagged, changes}], {1, q}, q];
    lambda = N@Coefficient[model, q, 1];
    (* ΔS = a + lambda S[t-1].  The intercept a shifts the equilibrium,
       but does not change the fitted lambda. *)
    If[NumberQ[lambda], result[[i]] = lambda],
    {i, lookback, n}];
  result
];

AACENXInverseRollingHalfLife[spread_List, lookback_Integer] :=
  Map[If[NumberQ[#] && -1 < # < 0, Log[1 + #]/Log[1/2], Indeterminate] &,
    AACENXRollingLambda[spread, lookback]];

AACENXDensityLab[] := DynamicModule[
  {tickerY = "SBLK", tickerX = "BHP", data = None, meanWindow = 20, beta = 1.,
   excursionThreshold = 1., halfLifeLookback = 60,
   status = "加载 pair 后计算 across、life 与 density。"},
  Column[{
    Style["across 事件、周期 life 与致密度", 24, Bold],
    Style["候选零穿越由 rolling z-score 的符号改变得到。某个候选点的左右两个相邻零穿越区间都必须出现 |z| > 阈值，才保留为 across 事件。相邻 across 事件的交易日距离为 life；定义 density = 1/life。", 12],
    Row[{"Y ", InputField[Dynamic[tickerY], String, FieldSize -> 6], " X ",
      InputField[Dynamic[tickerX], String, FieldSize -> 6], Spacer[8],
      Button["加载共同日线", data = AACENXDailyRead[tickerY, tickerX];
        status = If[Head[data] === Failure, data["Message"],
          With[{fit = AACENXEndpointBeta[data]}, beta = fit["Beta"];
            Row[{data["Y"], "/", data["X"], "：", Length[data["Dates"]], " 个共同交易日；默认 beta = ",
              NumberForm[beta, {6, 4}], "（端点去趋势残差方差最小）"}]]]],
      Spacer[12], "mean window ", Slider[Dynamic[meanWindow], {5, 60, 1}, ImageSize -> 130], Dynamic[meanWindow],
      Spacer[12], "|z| excursion threshold ", Slider[Dynamic[excursionThreshold], {.5, 2., .1}, ImageSize -> 130], Dynamic[excursionThreshold]}],
    Row[{"half-life lookback ", Slider[Dynamic[halfLifeLookback], {20, 180, 1}, ImageSize -> 180], Dynamic[halfLifeLookback],
      Spacer[12], Style["热力图将 density 与 1/rolling half-life 放在同一时间轴比较。", 11]}],
    Dynamic[Style[status, 11, Darker@Blue]],
    Style["确认时点：一个零穿越是否成为 across，要等右侧出现足够的 |z| excursion 才能确定。因此历史分段没有问题，但实盘标签应在确认后才产生，不能回填为零穿越当刻已知。", 12, Darker@Red],
    Dynamic[If[AssociationQ[data] && NumericQ[beta],
      Module[{result, dates, z, across, life, density, zPlot, densityPlot, rows, spread,
        inverseHalfLife, densitySeries, carriedDensity, heatRows, numericValues, low, high, heatData, heatPlot},
        result = AACENXAcrossEvents[data, meanWindow, beta, excursionThreshold];
        If[Head[result] === Failure, Return[Panel[result["Message"]]]];
        {dates, z, across, life, density} = Lookup[result, {"Dates", "ZScore", "Across", "Life", "Density"}];
        zPlot = DateListPlot[{Transpose[{dates, z}],
            Transpose[{dates[[across]], ConstantArray[0., Length[across]]}]}, Joined -> {True, False},
          PlotStyle -> {Directive[RGBColor[.55, .25, .85], Thick], Directive[Red, PointSize[.018]]},
          PlotLegends -> Placed[{"trailing rolling z-score", "confirmed across"}, Above],
          Frame -> True, FrameLabel -> {"date", "z-score"}, GridLines -> {None, {-excursionThreshold, 0, excursionThreshold}},
          GridLinesStyle -> Directive[GrayLevel[.75], Dashed], ImageSize -> 1050, AspectRatio -> .31,
          PlotLabel -> Style["across 事件：红点", 15, Bold]];
        densityPlot = If[Length[life] == 0, Panel["尚未有两个 confirmed across 事件，无法计算 life。"],
          DateListPlot[{Transpose[{dates[[Rest[across]]], life}], Transpose[{dates[[Rest[across]]], density}]}, Joined -> True,
            PlotStyle -> {Directive[RGBColor[.10, .45, .80], Thick], Directive[RGBColor[.10, .65, .35], Thick]},
            PlotLegends -> Placed[{"life (trading days)", "density = 1/life"}, Above], Frame -> True,
            FrameLabel -> {"date of later across", "value"}, GridLines -> Automatic, ImageSize -> 1050, AspectRatio -> .28]];
        spread = AACENXFixedSpread[data["PricesY"], data["PricesX"], beta]["Spread"];
        inverseHalfLife = AACENXInverseRollingHalfLife[spread, halfLifeLookback][[meanWindow ;;]];
        densitySeries = ConstantArray[Indeterminate, Length[dates]];
        Do[densitySeries[[across[[j + 1]]]] = density[[j]], {j, Length[density]}];
        carriedDensity = Rest@FoldList[If[NumericQ[#2], #2, #1] &, Indeterminate, densitySeries];
        heatRows = {carriedDensity, inverseHalfLife};
        numericValues = Cases[heatRows, _?NumericQ, Infinity];
        {low, high} = If[Length[numericValues] > 1 && Max[numericValues] > Min[numericValues],
          {Min[numericValues], Max[numericValues]}, {0., 1.}];
        heatData = Map[If[NumericQ[#], Rescale[#, {low, high}], -1.] &, heatRows, {2}];
        heatPlot = ArrayPlot[heatData, ColorRules -> {-1. -> White,
            x_ /; 0 <= x <= 1 :> ColorData["DarkRainbow"][x]}, Frame -> True,
          FrameTicks -> {{ {1, "density = 1/life"}, {2, "1/rolling half-life"}},
            Table[{i, DateString[dates[[i]], {"Year", "-", "Month"}]}, {i, 1, Length[dates], Max[1, Floor[Length[dates]/10]]}]},
          ImageSize -> 1050, AspectRatio -> .12,
          PlotLabel -> Style["时间热力图：白色 = 尚无可用值；颜色在两行共享 1/交易日尺度", 14, Bold]];
        rows = Table[<|"from across" -> DateString[dates[[across[[j]]]], {"Year", "-", "Month", "-", "Day"}],
          "to across" -> DateString[dates[[across[[j + 1]]]], {"Year", "-", "Month", "-", "Day"}],
          "life (trading days)" -> life[[j]], "density (1/day)" -> density[[j]]|>, {j, Length[life]}];
        Column[{zPlot, densityPlot, heatPlot, Dataset[rows]}, Spacings -> 1.1]
      ], Panel["加载 pair 后显示致密度。"]],
      TrackedSymbols :> {data, meanWindow, beta, excursionThreshold, halfLifeLookback}, SynchronousUpdating -> False]
  }, Spacings -> 1.1, BaseStyle -> {FontFamily -> "Noto Sans CJK SC"}]
];

AACENXHalfLifeSineLab[] := Manipulate[
  Module[{t, y, x, data, spread, result, zDates, z, inverseHalfLife, halfLife,
    finiteHalfLife, halfMin, halfMax, zMin, zMax, halfLifeScaled, halfLifeTicks,
    legsPlot, halfLifePlot, zPlot},
    t = Range[0, totalDays];
    y = 1 + yAmplitude Sin[2 Pi t/yPeriod];
    x = 1 + xAmplitude Sin[2 Pi t/xPeriod + phase];
    data = <|"Dates" -> t, "PricesY" -> y, "PricesX" -> x|>;
    spread = AACENXFixedSpread[y, x, beta]["Spread"];
    (* Reuse the trailing z-score calculation; this half-life panel does not
       display or use across/density events. *)
    result = AACENXAcrossEvents[data, meanWindow, beta, 1.];
    {zDates, z} = Lookup[result, {"Dates", "ZScore"}];
    inverseHalfLife = AACENXInverseRollingHalfLife[spread, halfLifeLookback][[meanWindow ;;]];
    halfLife = Map[If[NumberQ[#] && # > 0, 1/#, Indeterminate] &, inverseHalfLife];
    finiteHalfLife = Select[halfLife, NumberQ[#] && # > 0 &];
    {zMin, zMax} = MinMax[Join[{-3., 3.}, Select[z, NumberQ]]];
    If[Length[finiteHalfLife] >= 2,
      {halfMin, halfMax} = MinMax[finiteHalfLife];
      If[halfMin == halfMax, {halfMin, halfMax} = {Max[0., halfMin - 1.], halfMax + 1.}];
      halfLifeScaled = Map[If[NumberQ[#] && # > 0,
          Rescale[#, {halfMin, halfMax}, {zMin, zMax}], Indeterminate] &, halfLife];
      halfLifeTicks = ({Rescale[#, {halfMin, halfMax}, {zMin, zMax}], NumberForm[#, {6, 2}]} & /@
          FindDivisions[{halfMin, halfMax}, 5]),
      halfLifeScaled = ConstantArray[Indeterminate, Length[halfLife]];
      halfLifeTicks = {}
    ];
    legsPlot = ListLinePlot[{Transpose[{t, y}], Transpose[{t, x}]},
      PlotStyle -> {Directive[Blue, Thick], Directive[Orange, Thick]},
      PlotLegends -> Placed[{"Y leg", "X leg"}, Above], Frame -> True,
      FrameLabel -> {"trading day", "normalised leg"}, GridLines -> Automatic,
      ImageSize -> 1050, AspectRatio -> .24,
      PlotLabel -> Style["两条独立正弦腿", 15, Bold]];
    halfLifePlot = ListLinePlot[Transpose[{zDates, halfLife}],
      PlotStyle -> Directive[RGBColor[.1, .45, .8], Thick],
      PlotLegends -> Placed[{"rolling half-life"}, Above], Frame -> True,
      FrameLabel -> {"trading day", "half-life (trading days)"}, GridLines -> Automatic,
      ImageSize -> 1050, AspectRatio -> .28,
      PlotLabel -> Style["rolling half-life", 15, Bold]];
    zPlot = ListLinePlot[{Transpose[{zDates, z}], Transpose[{zDates, halfLifeScaled}]}, Joined -> True,
      PlotStyle -> {Directive[RGBColor[.55, .25, .85], Thick], Directive[RGBColor[.1, .45, .8], Thick]},
      PlotLegends -> Placed[{"trailing z-score (left axis)", "rolling half-life (right axis)"}, Above],
      Frame -> True, FrameLabel -> {{"z-score", "half-life (trading days)"}, {"trading day", None}},
      FrameTicks -> {{Automatic, halfLifeTicks}, {Automatic, None}}, PlotRange -> {All, {zMin, zMax}},
      GridLines -> {None, {-1., 0., 1.}},
      GridLinesStyle -> Directive[GrayLevel[.7], Dashed], ImageSize -> 1050, AspectRatio -> .28,
      PlotLabel -> Style["trailing z-score 与 rolling half-life 对比", 15, Bold]];
    Column[{legsPlot, halfLifePlot, zPlot}, Spacings -> 1.1]
  ],
  {{beta, .8, "fixed beta"}, 0, 2, .01, Appearance -> "Labeled"},
  Delimiter,
  {{yAmplitude, .25, "Y amplitude"}, .02, .60, .01, Appearance -> "Labeled"},
  {{yPeriod, 40, "Y period (days)"}, 10, 120, 1, Appearance -> "Labeled"},
  {{xAmplitude, .35, "X amplitude"}, .02, .60, .01, Appearance -> "Labeled"},
  {{xPeriod, 40, "X period (days)"}, 10, 120, 1, Appearance -> "Labeled"},
  {{phase, 0, "X phase (radians)"}, 0, 2 Pi, Pi/20, Appearance -> "Labeled"},
  Delimiter,
  {{meanWindow, 20, "mean window"}, 5, 60, 1, Appearance -> "Labeled"},
  {{halfLifeLookback, 60, "half-life lookback"}, 20, 160, 1, Appearance -> "Labeled"},
  {{totalDays, 400, "simulation length"}, 150, 900, 10, Appearance -> "Labeled"},
  SaveDefinitions -> True, SynchronousUpdating -> False
];

AACENXOneCycleHalfLifeDemo[] := Manipulate[
  Module[{theta, y, x, spread, mu, deviation, delta, instantaneousLambda,
    rollingLambda, halfLife, specialTheta, legsPlot, deviationPlot, lambdaPlot, halfLifePlot, specialPointTable},
    theta = Subdivide[0., 2 Pi, samples];
    y = 1 + yAmplitude Sin[theta];
    x = 1 + xAmplitude Sin[theta];
    spread = AACENXFixedSpread[y, x, beta]["Spread"];
    mu = Mean[spread];
    deviation = spread - mu;
    delta = Differences[spread];
    (* This is a one-step direction diagnostic: ΔS/(S-mu).  It is not a
       regression estimate, and becomes undefined at the equilibrium. *)
    instantaneousLambda = MapThread[
      If[Abs[#2] < 10^-8, Indeterminate, #1/#2] &, {delta, Most[deviation]}];
    rollingLambda = AACENXRollingLambda[spread, rollingWindow];
    halfLife = Map[If[NumberQ[#] && -1 < # < 0, Log[1/2]/Log[1 + #], Indeterminate] &, rollingLambda];
    specialTheta = {Pi/4, Pi/2, 3 Pi/4, Pi, 5 Pi/4, 3 Pi/2, 7 Pi/4, 2 Pi};
    legsPlot = ListLinePlot[{Transpose[{theta, y}], Transpose[{theta, x}]},
      PlotStyle -> {Directive[Blue, Thick], Directive[Orange, Thick]},
      PlotLegends -> Placed[{"Y", "X"}, Above], Frame -> True,
      FrameLabel -> {"phase theta", "normalised leg"},
      FrameTicks -> {{Automatic, None}, {{{0, "0"}, {Pi/2, "pi/2"}, {Pi, "pi"}, {3 Pi/2, "3pi/2"}, {2 Pi, "2pi"}}, None}},
      GridLines -> {{Pi/2, Pi, 3 Pi/2, 2 Pi}, None}, ImageSize -> 1050, AspectRatio -> .22,
      PlotLabel -> Style["一个周期：0 到 2 pi", 15, Bold]];
    deviationPlot = ListLinePlot[Transpose[{theta, deviation}], PlotStyle -> Directive[RGBColor[.05, .78, .95], Thick],
      Frame -> True, FrameLabel -> {"phase theta", "S - mean(S)"},
      FrameTicks -> {{Automatic, None}, {{{0, "0"}, {Pi/2, "pi/2"}, {Pi, "pi"}, {3 Pi/2, "3pi/2"}, {2 Pi, "2pi"}}, None}},
      GridLines -> {{Pi/2, Pi, 3 Pi/2, 2 Pi}, {0}}, GridLinesStyle -> Directive[RGBColor[.45, .65, .82], Dashed], ImageSize -> 1050, AspectRatio -> .22,
      PlotLabel -> Style[Row[{"spread 相对平衡位置的偏离   (mu = ", NumberForm[mu, {5, 3}], ")"}], 15, Bold]];
    lambdaPlot = ListLinePlot[{Transpose[{Rest[theta], instantaneousLambda}], Transpose[{theta, rollingLambda}]},
      PlotStyle -> {Directive[RGBColor[1., .72, .08], Thick], Directive[RGBColor[1., .25, .55], Thick]},
      PlotLegends -> Placed[{"one-step ΔS/(S-mean), 仅作方向参考", "trailing rolling lambda"}, Above],
      Frame -> True, FrameLabel -> {"phase theta", "lambda"},
      FrameTicks -> {{Automatic, None}, {{{0, "0"}, {Pi/2, "pi/2"}, {Pi, "pi"}, {3 Pi/2, "3pi/2"}, {2 Pi, "2pi"}}, None}},
      GridLines -> {{Pi/2, Pi, 3 Pi/2, 2 Pi}, {0}}, GridLinesStyle -> Directive[RGBColor[.45, .65, .82], Dashed], ImageSize -> 1050, AspectRatio -> .25,
      PlotLabel -> Style["瞬时方向与 trailing rolling lambda（第二张图）", 15, Bold]];
    halfLifePlot = ListLinePlot[Transpose[{theta, halfLife}], PlotStyle -> Directive[RGBColor[.1, .45, .8], Thick],
      PlotLegends -> Placed[{"rolling half-life; only where -1 < rolling lambda < 0"}, Above], Frame -> True,
      FrameLabel -> {"phase theta", "half-life (samples)"},
      FrameTicks -> {{Automatic, None}, {{{0, "0"}, {Pi/2, "pi/2"}, {Pi, "pi"}, {3 Pi/2, "3pi/2"}, {2 Pi, "2pi"}}, None}},
      GridLines -> {{Pi/2, Pi, 3 Pi/2, 2 Pi}, None}, ImageSize -> 1050, AspectRatio -> .22,
      PlotLabel -> Style["rolling half-life：lambda >= 0 的位置留空", 15, Bold]];
    specialPointTable = Grid[
      Prepend[Table[{TraditionalForm[th],
          Which[PossibleZeroQ[Sin[th]], "在平衡位置，方向比值无定义", Sin[th] Cos[th] > 0, "lambda 瞬时为正：继续远离",
            True, "lambda 瞬时为负：正在拉回"]}, {th, specialTheta}], {"theta", "相对均值的局部方向"}],
      Frame -> All, Alignment -> Left, Background -> {None, {Lighter[Blue, .9]}}];
    Column[{legsPlot, lambdaPlot, deviationPlot, halfLifePlot,
      Style["特殊点（以 S-mean(S) 为中心）", 13, Bold], specialPointTable}, Spacings -> 1.05]
  ],
  {{beta, .8, "fixed beta"}, 0, 2, .01, Appearance -> "Labeled"},
  {{yAmplitude, .25, "Y amplitude"}, .02, .60, .01, Appearance -> "Labeled"},
  {{xAmplitude, .35, "X amplitude"}, .02, .60, .01, Appearance -> "Labeled"},
  {{rollingWindow, 16, "rolling lambda window (samples)"}, 6, 40, 1, Appearance -> "Labeled"},
  {{samples, 80, "samples over 0..2pi"}, 40, 160, 4, Appearance -> "Labeled"},
  SaveDefinitions -> True, SynchronousUpdating -> False
];

AACENXLambdaHatFiveSteps[] := Manipulate[
  Module[{theta, spread, safeStart, window, x, y, xIndex, yIndex, xMean, yMean, xCentered, yCentered, cross, xSquared, cumulativeLambda,
    lambdaHat, intercept, mu, headers, rows, table, chart, stepText},
    (* 101 smooth samples indexed 0..100.  Subdivide keeps all quarter-cycle
       landmarks exactly: 0, pi/4, pi/2, 3 pi/4 and 2 pi. *)
    theta = Subdivide[0., 2 Pi, 100];
    spread = spreadMean + amplitude Sin[theta];
    safeStart = Min[Max[windowStart, 0], Length[spread] - windowLength];
    (* UI indices are 0-based; Mathematica Part indices are 1-based. *)
    window = spread[[safeStart + 1 ;; safeStart + windowLength]];
    x = Most[window];
    y = Differences[window];
    xIndex = Range[safeStart, safeStart + windowLength - 2];
    yIndex = Range[safeStart + 1, safeStart + windowLength - 1];
    xMean = Mean[x]; yMean = Mean[y];
    xCentered = x - xMean; yCentered = y - yMean;
    cross = xCentered yCentered; xSquared = xCentered^2;
    lambdaHat = Total[cross]/Total[xSquared];
    (* Expanding-window OLS: every point recomputes both column means before
       calculating its own slope, rather than accumulating fixed cross terms. *)
    cumulativeLambda = Join[{Indeterminate}, Table[
        With[{xp = Take[x, k], yp = Take[y, k]},
          Total[(xp - Mean[xp]) (yp - Mean[yp])]/Total[(xp - Mean[xp])^2]],
        {k, 2, Length[y]}]];
    intercept = yMean - lambdaHat xMean;
    mu = If[PossibleZeroQ[lambdaHat], Indeterminate, -intercept/lambdaHat];
    headers = Switch[step,
      1, {"pair", "x index: t-1", "x = S[t-1]", "y index: t", "y = Delta S[t]"},
      2, {"pair", "x index", "y index", "x", "y", "x - mean(x)", "y - mean(y)"},
      3, {"pair", "x index", "y index", "x centered", "y centered"},
      4, {"pair", "x index", "y index", "x centered", "y centered", "cross = x centered * y centered"},
      _, {"last y index: t", "expanding-window OLS lambda hat"}
    ];
    rows = Switch[step,
      1, Table[{i, xIndex[[i]], x[[i]], yIndex[[i]], y[[i]]}, {i, Length[y]}],
      2, Table[{i, xIndex[[i]], yIndex[[i]], x[[i]], y[[i]], xCentered[[i]], yCentered[[i]]}, {i, Length[y]}],
      3, Table[{i, xIndex[[i]], yIndex[[i]], xCentered[[i]], yCentered[[i]]}, {i, Length[y]}],
      4, Table[{i, xIndex[[i]], yIndex[[i]], xCentered[[i]], yCentered[[i]], cross[[i]]}, {i, Length[y]}],
      _, Table[{yIndex[[i]], cumulativeLambda[[i]]}, {i, 2, Length[y]}]
    ];
    table = Pane[Grid[Prepend[Map[NumberForm[#, {7, 4}] &, #, {2}] & /@ rows, headers],
       Frame -> All, Alignment -> Right, Background -> {None, {RGBColor[.72, .9, 1.]}}], {1050, 250}, Scrollbars -> True];
    chart = Switch[step,
      1, Column[{
        ListLinePlot[Transpose[{xIndex, x}], PlotStyle -> Directive[RGBColor[.05, .78, .95], Thick],
          PlotLegends -> Placed[{"x = yesterday spread"}, Above], Frame -> True,
          FrameLabel -> {"point index (t-1)", "spread"}, ImageSize -> 1050, AspectRatio -> .18],
        ListLinePlot[Transpose[{yIndex, y}], PlotStyle -> Directive[RGBColor[1., .72, .08], Thick],
          PlotLegends -> Placed[{"y = today change"}, Above], Frame -> True,
          FrameLabel -> {"point index (t)", "Delta S"}, GridLines -> {None, {0}},
          GridLinesStyle -> Directive[RGBColor[.45, .65, .82], Dashed], ImageSize -> 1050, AspectRatio -> .18]
        }, Spacings -> .7],
      2, Column[{
        ListLinePlot[Transpose[{xIndex, xCentered}], PlotStyle -> Directive[RGBColor[.05, .78, .95], Thick],
          PlotLegends -> Placed[{"x centered"}, Above], Frame -> True,
          FrameLabel -> {"point index (t-1)", "x - mean(x)"}, GridLines -> {None, {0}},
          GridLinesStyle -> Directive[RGBColor[.45, .65, .82], Dashed], ImageSize -> 1050, AspectRatio -> .18],
        ListLinePlot[Transpose[{yIndex, yCentered}], PlotStyle -> Directive[RGBColor[1., .72, .08], Thick],
          PlotLegends -> Placed[{"y centered"}, Above], Frame -> True,
          FrameLabel -> {"point index (t)", "y - mean(y)"}, GridLines -> {None, {0}},
          GridLinesStyle -> Directive[RGBColor[.45, .65, .82], Dashed], ImageSize -> 1050, AspectRatio -> .18]
        }, Spacings -> .7],
      3, ListPlot[Transpose[{xCentered, yCentered}], PlotStyle -> Directive[RGBColor[1., .25, .55], PointSize[.018]],
        Frame -> True, FrameLabel -> {"x centered = S[t-1] - mean(x)", "y centered = Delta S[t] - mean(y)"},
        GridLines -> { {0}, {0}}, GridLinesStyle -> Directive[RGBColor[.45, .65, .82], Dashed],
        ImageSize -> 1050, AspectRatio -> .34,
        PlotLabel -> Style["centered relation: 点云向右下倾斜时，lambda hat 为负", 15, Bold]],
      4, ListLinePlot[Transpose[{yIndex, cross}], PlotStyle -> Directive[RGBColor[1., .25, .55], Thick], Frame -> True,
        PlotLegends -> Placed[{"cross per row"}, Above], GridLines -> {None, {0}}, GridLinesStyle -> Directive[RGBColor[.45, .65, .82], Dashed], ImageSize -> 1050, AspectRatio -> .22],
      _, ListLinePlot[Transpose[{Rest[yIndex], Rest[cumulativeLambda]}], PlotStyle -> Directive[RGBColor[1., .25, .55], Thick],
        PlotLegends -> Placed[{"expanding-window OLS lambda hat"}, Above], Frame -> True,
        FrameLabel -> {"last included y index t", "OLS lambda hat"}, GridLines -> {None, {0}},
        GridLinesStyle -> Directive[RGBColor[.45, .65, .82], Dashed], ImageSize -> 1050, AspectRatio -> .28,
        PlotLabel -> Style["随着样本对逐个加入，OLS lambda hat 如何形成", 15, Bold]]
    ];
    stepText = Switch[step,
      1, "第 1 步：只把窗口拆成两列——昨日水平 x 与今天变化 y。",
      2, "第 2 步：从每列各自减去窗口平均值；现在只比较『比本窗口正常水平高／低』。",
      3, "第 3 步：把中心化后的 x、y 配成散点。点云向右下倾斜，说明高水平通常配低变化，是均值回复。",
      4, "第 4 步：逐行相乘。cross 为负表示该行支持均值回复；正表示该行支持动量。",
      _, "第 5 步：从第一个 (x,y) 样本对开始逐个加入；每一次都重新做带截距 OLS，直到最后一个点得到完整窗口的 lambda hat。"
    ];
    Column[{Style[stepText, 13, RGBColor[.05, .45, .75]], chart, table,
      If[step == 5, Panel[Style[Row[{"final full-window OLS lambda hat = ", NumberForm[lambdaHat, {8, 5}],
        "     local mu = ", NumberForm[mu, {8, 5}]}], 13, RGBColor[1., .25, .55]],
        Background -> RGBColor[.9, .96, 1.]], Nothing]}, Spacings -> 1.]
  ],
  {{step, 1, "show step"}, {1 -> "1. x and y", 2 -> "2. subtract means", 3 -> "3. centered relation", 4 -> "4. cross", 5 -> "5. divide to get lambda hat"}, SetterBar},
  {{spreadMean, .20, "spread mean (mu)"}, .05, .50, .01, Appearance -> "Labeled"},
  {{amplitude, .03, "spread amplitude"}, .01, .15, .005, Appearance -> "Labeled"},
  {{windowStart, 0, "window start (0-based index)"}, 0, Dynamic[101 - windowLength], 1, Appearance -> "Labeled"},
  {{windowLength, 101, "window length (spread observations)"}, 6, 101, 1, Appearance -> "Labeled"},
  SaveDefinitions -> True, SynchronousUpdating -> False
];

AACENXOLSGeometryDemo[] := Manipulate[
  Module[{theta, spread, t, x, y, xMean, yMean, xCentered, yCentered, lambdaHat, intercept,
    xMin, xMax, yMin, yMax, xyPlot, txPlot, tyPlot, xyzPlot},
    theta = Subdivide[0., 2 Pi, sampleIntervals];
    spread = spreadMean + amplitude Sin[theta];
    t = Rest[theta];
    x = Most[spread];
    y = Differences[spread];
    xMean = Mean[x]; yMean = Mean[y];
    xCentered = x - xMean; yCentered = y - yMean;
    lambdaHat = Total[xCentered yCentered]/Total[xCentered^2];
    intercept = yMean - lambdaHat xMean;
    {xMin, xMax} = MinMax[x]; {yMin, yMax} = MinMax[y];
    xyPlot = Show[
      ListPlot[Transpose[{x, y}], PlotStyle -> Directive[RGBColor[.05, .78, .95], PointSize[.018]],
        Frame -> True, FrameLabel -> {"x = S[t-1]", "y = Delta S[t]"},
        GridLines -> {{xMean}, {yMean}}, GridLinesStyle -> Directive[RGBColor[.45, .65, .82], Dashed],
        ImageSize -> 760, AspectRatio -> .55],
      Plot[intercept + lambdaHat q, {q, xMin, xMax}, PlotStyle -> Directive[RGBColor[1., .25, .55], Thick]],
      PlotLabel -> Style["二维 OLS：青点样本，粉线 y = a + lambda hat x", 15, Bold]
    ];
    txPlot = ListLinePlot[Transpose[{t, x}], PlotStyle -> Directive[RGBColor[.05, .78, .95], Thick],
      Frame -> True, FrameLabel -> {"t (phase)", "x = S[t-1]"},
      GridLines -> {{Pi/2, Pi, 3 Pi/2, 2 Pi}, {xMean}}, GridLinesStyle -> Directive[RGBColor[.45, .65, .82], Dashed],
      ImageSize -> 760, AspectRatio -> .32, PlotLabel -> Style["t-x 平面：一个周期的正弦 x(t)", 14, Bold]];
    tyPlot = ListLinePlot[Transpose[{t, y}], PlotStyle -> Directive[RGBColor[1., .72, .08], Thick],
      Frame -> True, FrameLabel -> {"t (phase)", "y = Delta S[t]"},
      GridLines -> {{Pi/2, Pi, 3 Pi/2, 2 Pi}, {0}}, GridLinesStyle -> Directive[RGBColor[.45, .65, .82], Dashed],
      ImageSize -> 760, AspectRatio -> .32, PlotLabel -> Style["t-y 平面：差分 y(t)，近似余弦", 14, Bold]];
    xyzPlot = Show[
      Plot3D[intercept + lambdaHat q, {u, Min[t], Max[t]}, {q, xMin, xMax},
        PlotStyle -> Directive[RGBColor[1., .25, .55], Opacity[.25]], Mesh -> None, BoundaryStyle -> None],
      Graphics3D[{
        Directive[RGBColor[.05, .78, .95], Thick], Line[Transpose[{t, x, ConstantArray[yMin, Length[t]]}]],
        Directive[RGBColor[1., .72, .08], Thick], Line[Transpose[{t, ConstantArray[xMin, Length[t]], y}]]
      }],
      ListLinePlot3D[Transpose[{t, x, y}], PlotStyle -> Directive[RGBColor[1., .72, .08], Thick]],
      ListPointPlot3D[Transpose[{t, x, y}], PlotStyle -> Directive[RGBColor[.05, .78, .95], PointSize[.016]]],
      AxesLabel -> {"t (phase)", "x = S[t-1]", "y = Delta S[t]"}, BoxRatios -> {1.4, 1, .9},
      PlotRange -> {{Min[t], Max[t]}, {xMin, xMax}, {yMin, yMax}}, ImageSize -> 760,
      PlotLabel -> Style["三维图：青色投影在 t-x 平面；黄色投影在 t-y 平面", 15, Bold]
    ];
    Column[{
      Style["先看 t-x 与 t-y 的两条时间曲线，再看右下角三维空间；最后回到左上角 (x,y) 投影理解 OLS。", 13, RGBColor[.05, .45, .75]],
      Grid[{{xyPlot, xyzPlot}}, Spacings -> 1.2],
      Grid[{{txPlot, tyPlot}}, Spacings -> 1.2],
      Style[Row[{"lambda hat = ", NumberForm[lambdaHat, {8, 6}],
        "     其含义：在 OLS 中，x 每高 1 个单位，y 的最佳线性预测平均改变 lambda hat 个单位。粉线 / 透明平面都是 y = a + lambda hat x；平面沿 t 方向延伸，说明该模型没有使用 t。"}], 12, RGBColor[1., .25, .55]]
    }, Spacings -> 1.]
  ],
  {{spreadMean, .20, "spread mean (mu)"}, .05, .50, .01, Appearance -> "Labeled"},
  {{amplitude, .03, "spread amplitude"}, .01, .15, .005, Appearance -> "Labeled"},
  {{sampleIntervals, 100, "phase intervals over 0..2pi"}, 20, 180, 4, Appearance -> "Labeled"},
  SaveDefinitions -> True, SynchronousUpdating -> False
];

AACENXSlowingSineLambdaDemo[] := Manipulate[
  Module[{tEnd, tTime, phaseTime, phaseEqual, tEqual, spreadTime, spreadEqual,
    contractingLambda, lambdaTime, lambdaEqual, spreadPlot, phaseSpeedPlot, timeLambdaPlot, equalLambdaPlot},
    (* phi(t)=t^3: phase speed phi'(t)=3 t^2 accelerates strongly, so periods
       become shorter.  Both samplings observe this same underlying curve. *)
    tEnd = (2 Pi cycles)^(1/3);
    tTime = Subdivide[0., tEnd, timeIntervals];
    phaseTime = tTime^3;
    phaseEqual = (2 Pi/pointsPerCycle) Range[0, cycles pointsPerCycle];
    tEqual = phaseEqual^(1/3);
    spreadTime = spreadMean + amplitude Sin[phaseTime];
    spreadEqual = spreadMean + amplitude Sin[phaseEqual];
    contractingLambda[values_] := Module[{dev, change, raw},
      dev = values - spreadMean; change = Differences[values];
      raw = Prepend[MapThread[If[Abs[#2] < 10^-10, Indeterminate, #1/#2] &, {change, Most[dev]}], Indeterminate];
      Map[If[NumberQ[#] && -1 < # < 0, #, Indeterminate] &, raw]
    ];
    lambdaTime = contractingLambda[spreadTime];
    lambdaEqual = contractingLambda[spreadEqual];
    spreadPlot = ListLinePlot[Transpose[{tTime, spreadTime}], PlotStyle -> Directive[RGBColor[.05, .78, .95], Thick],
      Frame -> True, FrameLabel -> {"physical time t", "x(t) = mean + A Sin[t^3]"},
      ImageSize -> 1050, AspectRatio -> .22, PlotLabel -> Style["同一条变频正弦：phi(t) = t^3", 15, Bold]];
    phaseSpeedPlot = ListLinePlot[Transpose[{tTime, 3 tTime^2}], PlotStyle -> Directive[RGBColor[.35, .95, .55], Thick],
      Frame -> True, FrameLabel -> {"physical time t", "phi'(t) = 3 t^2"},
      ImageSize -> 1050, AspectRatio -> .18, PlotLabel -> Style["相位速度上升：后期周期更短", 14, Bold]];
    timeLambdaPlot = ListLinePlot[Transpose[{tTime, lambdaTime}], PlotStyle -> Directive[RGBColor[1., .25, .55], Thick],
      PlotLegends -> Placed[{"equal-time instant lambda (K-line style)"}, Above],
      Frame -> True, FrameLabel -> {"physical time t", "instant lambda per equal-time sample"}, GridLines -> {None, {0}},
      GridLinesStyle -> Directive[RGBColor[.45, .65, .82], Dashed], ImageSize -> 1050, AspectRatio -> .24,
      PlotLabel -> Style["等时间采样：后期每周期自然包含更少样本", 15, Bold]];
    equalLambdaPlot = ListLinePlot[Transpose[{tEqual, lambdaEqual}], PlotStyle -> Directive[RGBColor[1., .72, .08], Thick],
      PlotLegends -> Placed[{"equal-phase instant lambda"}, Above],
      Frame -> True, FrameLabel -> {"physical time t", "instant lambda per equal-phase sample"}, GridLines -> {None, {0}},
      GridLinesStyle -> Directive[RGBColor[.45, .65, .82], Dashed], ImageSize -> 1050, AspectRatio -> .24,
      PlotLabel -> Style["等相位采样：每个周期固定相同的样本数", 15, Bold]];
    Column[{spreadPlot, phaseSpeedPlot, timeLambdaPlot, equalLambdaPlot,
      Style["两图均只保留 -1 < Delta D/D[t-1] < 0，且都没有 rolling window 或 half-life。粉色模拟实际 K 线的等时间 bars；黄色是受控的等相位对照。", 12, RGBColor[1., .25, .55]]}, Spacings -> 1.]
  ],
  {{spreadMean, .20, "spread mean"}, .05, .50, .01, Appearance -> "Labeled"},
  {{amplitude, .03, "amplitude"}, .01, .15, .005, Appearance -> "Labeled"},
  {{cycles, 8, "number of cycles"}, 2, 16, 1, Appearance -> "Labeled"},
  {{pointsPerCycle, 100, "equal samples per cycle"}, 20, 180, 4, Appearance -> "Labeled"},
  {{timeIntervals, 800, "equal-time intervals"}, 160, 1600, 20, Appearance -> "Labeled"},
  SaveDefinitions -> True, SynchronousUpdating -> False
];

AACENXSineSpreadLab[] := Manipulate[
  Module[{days, aa, cenx, spread, mean, meanDays, legsPlot, spreadPlot, betaStar,
    yLow, yHigh},
    days = Range[0, 240];
    aa = 1 + aaAmplitude (1 + aaAmplitudeSlope days/Max[days]) Sin[2 Pi days/aaPeriod];
    cenx = 1 + cenxAmplitude (1 + cenxAmplitudeSlope days/Max[days]) Sin[2 Pi days/cenxPeriod + phase];
    spread = aa - beta cenx;
    mean = MovingAverage[spread, meanWindow];
    meanDays = days[[meanWindow ;;]];
    yLow = spreadCenter - spreadHalfRange;
    yHigh = spreadCenter + spreadHalfRange;
    betaStar = If[aaPeriod == cenxPeriod && phase == 0 && aaAmplitudeSlope == cenxAmplitudeSlope,
      aaAmplitude/cenxAmplitude, Missing["DependsOnTime"]];
    legsPlot = ListLinePlot[
      {Transpose[{days, aa}], Transpose[{days, cenx}]}, Joined -> True,
      PlotStyle -> {Directive[RGBColor[.12, .27, .51], Thick], Directive[RGBColor[.95, .40, 0], Thick]},
      PlotLegends -> Placed[{"AA leg", "CENX leg"}, Above], Frame -> True,
      FrameLabel -> {"trading day", "simulated leg price"}, GridLines -> {None, Automatic},
      GridLinesStyle -> Directive[GrayLevel[.87]], PlotRange -> All, ImageSize -> 1050, AspectRatio -> .23,
      PlotLabel -> Style["两条模拟腿", 15, Bold]
    ];
    spreadPlot = ListLinePlot[
      {Transpose[{days, spread}], Transpose[{meanDays, mean}]}, Joined -> True,
      PlotStyle -> {Directive[RGBColor[.25, .65, 1.], Thick], Directive[White, Thick]},
      PlotLegends -> Placed[Style[#, White] & /@ {"spread = AA - beta CENX",
         ToString[meanWindow] <> "d trailing mean (white)"}, Above],
      Background -> RGBColor[.055, .07, .10], Frame -> True, FrameStyle -> White,
      FrameLabel -> {"trading day", "spread"}, LabelStyle -> White, GridLines -> {None, Automatic},
      GridLinesStyle -> Directive[GrayLevel[.32]], PlotRange -> {All, {yLow, yHigh}}, ImageSize -> 1050, AspectRatio -> .30,
      PlotLabel -> Style[If[NumericQ[betaStar],
         "同频同相时，完全抵消的 beta = " <> ToString@NumberForm[betaStar, {4, 3}],
         "频率或相位不一致：不存在一个全程完全抵消的固定 beta"] <>
        "  |  fixed y range: [" <> ToString@NumberForm[yLow, {5, 2}] <> ", " <>
        ToString@NumberForm[yHigh, {5, 2}] <> "]", 15, Bold, White]
    ];
    Column[{legsPlot, spreadPlot}, Spacings -> 1.1]
  ],
  {{beta, .6, "fixed beta"}, 0, 1.2, .01, Appearance -> "Labeled"},
  Delimiter,
  {{aaAmplitude, .30, "AA amplitude"}, .05, .60, .01, Appearance -> "Labeled"},
  {{aaAmplitudeSlope, 0., "AA amplitude slope"}, -.9, 1.5, .05, Appearance -> "Labeled"},
  {{cenxAmplitude, .50, "CENX amplitude"}, .05, .80, .01, Appearance -> "Labeled"},
  {{cenxAmplitudeSlope, 0., "CENX amplitude slope"}, -.9, 1.5, .05, Appearance -> "Labeled"},
  Delimiter,
  {{aaPeriod, 20, "AA period (days)"}, 10, 40, 1, Appearance -> "Labeled"},
  {{cenxPeriod, 20, "CENX period (days)"}, 10, 40, 1, Appearance -> "Labeled"},
  {{phase, 0, "CENX phase (radians)"}, 0, 2 Pi, Pi/20, Appearance -> "Labeled"},
  Delimiter,
  {{meanWindow, 20, "spread mean window"}, 2, 80, 1, Appearance -> "Labeled"},
  {{spreadCenter, .5, "spread y-axis centre"}, -2, 3, .05, Appearance -> "Labeled"},
  {{spreadHalfRange, 1., "spread y-axis half-range"}, .1, 3, .05, Appearance -> "Labeled"},
  SaveDefinitions -> True,
  SynchronousUpdating -> False
];
