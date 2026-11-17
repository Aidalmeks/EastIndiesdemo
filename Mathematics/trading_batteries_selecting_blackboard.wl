(* Read-only fixed-beta blackboard. Raw prices are never rebased. *)

ClearAll[TBSQL, TBPairs, TBRead, TBBeta, TBIndicators, TBKappa, TBEquity, TBBoard];

TBSQL[sql_String] := Quiet@Check[
  ImportString[
    RunProcess[{"/usr/bin/mysql", "-uquant", "-p1q2w3e", "--batch",
      "--skip-column-names", "quant_data", "-e", sql}, "StandardOutput"], "TSV"],
  $Failed
];

TBPairs[] := Module[{rows = TBSQL[
   "SELECT pair_name,ticker_a,ticker_b FROM metapairs WHERE COALESCE(is_active,1)=1 ORDER BY pair_name"]},
  If[rows === $Failed, Return[<||>]];
  Association@Map[ToString[#[[1]]] -> (ToUpperCase /@ ToString /@ #[[2 ;; 3]]) &,
    Select[rows, Length[#] == 3 && And @@ (StringMatchQ[ToString[#], LetterCharacter ..] & /@ #[[2 ;; 3]]) &]]
];

TBRead[y0_String, x0_String, frequency_String] := Module[{y, x, sql, rows, number, date, clean},
  y = ToUpperCase@StringTrim[y0]; x = ToUpperCase@StringTrim[x0];
  If[y === x || !StringMatchQ[y, LetterCharacter ..] || !StringMatchQ[x, LetterCharacter ..], Return[$Failed]];
  sql = If[frequency === "日线",
    "SELECT a.trade_date,a.price,b.price FROM us_market_days a JOIN us_market_days b ON b.ticker='" <> x <>
      "' AND b.trade_date=a.trade_date WHERE a.ticker='" <> y <>
      "' AND a.price IS NOT NULL AND b.price IS NOT NULL ORDER BY a.trade_date",
    "SELECT a.time_utc,a.price,b.price FROM us_market_prices a JOIN us_market_prices b ON b.ticker='" <> x <>
      "' AND b.time_utc=a.time_utc WHERE a.ticker='" <> y <>
      "' AND a.price IS NOT NULL AND b.price IS NOT NULL ORDER BY a.time_utc"];
  rows = TBSQL[sql]; If[rows === $Failed, Return[$Failed]];
  number[v_] := Quiet@Check[N@ToExpression[ToString[v], InputForm], Indeterminate];
  date[v_] := If[Head[v] === DateObject, v, DateObject[ToString[v]]];
  clean = Select[({date[#[[1]]], number[#[[2]]], number[#[[3]]]} &) /@ rows,
    NumericQ[#[[2]]] && NumericQ[#[[3]]] &];
  If[Length[clean] < 30, Return[$Failed]];
  <|"Times" -> clean[[All, 1]], "Y" -> y, "X" -> x,
    "PY" -> clean[[All, 2]], "PX" -> clean[[All, 3]], "Frequency" -> frequency|>
];

TBBeta[y_List, x_List] := Module[{n = Length[y], dy, dx},
  dy = y - (First[y] + (Last[y] - First[y]) Range[0, n - 1]/(n - 1));
  dx = x - (First[x] + (Last[x] - First[x]) Range[0, n - 1]/(n - 1));
  If[Dot[dx, dx] <= 0, 1., Dot[dx, dy]/Dot[dx, dx]]
];

TBIndicators[d_Association, beta_?NumericQ, window_Integer] := Module[{s, m, sig, z, pad, dev},
  s = d["PY"] - beta d["PX"]; pad = ConstantArray[Indeterminate, window - 1];
  dev[v_] := Sqrt[Mean[(v - Mean[v])^2]];
  m = Join[pad, Mean /@ Partition[s, window, 1]];
  sig = Join[pad, dev /@ Partition[s, window, 1]];
  z = MapThread[If[NumericQ[#2] && #2 > 0, (#1 - #3)/#2, Indeterminate] &, {s, sig, m}];
  Join[d, <|"Beta" -> beta, "Spread" -> s, "Mean" -> m, "Sigma" -> sig, "Z" -> z|>]
];

TBKappa[z_List, window_Integer, neutral_: .25] := Module[
  {dist, out, valid, termN, termD, prefixN, prefixD, prefixCount, num, den, count, n = Length[z], i},
  dist = Map[If[NumericQ[#], Max[Abs[#] - neutral, 0.], Indeterminate] &, z];
  valid = Table[i > 1 && NumericQ[dist[[i]]] && NumericQ[dist[[i - 1]]] &&
    dist[[i - 1]] > 0 && !(z[[i - 1]] z[[i]] <= 0 && Abs[z[[i - 1]]] >= neutral), {i, n}];
  termN = Table[If[valid[[i]], dist[[i - 1]] (dist[[i]] - dist[[i - 1]]), 0.], {i, n}];
  termD = Table[If[valid[[i]], dist[[i - 1]]^2, 0.], {i, n}];
  prefixN = Prepend[Accumulate[termN], 0.];
  prefixD = Prepend[Accumulate[termD], 0.];
  prefixCount = Prepend[Accumulate[Boole /@ valid], 0];
  out = ConstantArray[Indeterminate, n];
  Do[
    num = prefixN[[i + 1]] - prefixN[[i - window + 1]];
    den = prefixD[[i + 1]] - prefixD[[i - window + 1]];
    count = prefixCount[[i + 1]] - prefixCount[[i - window + 1]];
    If[count >= Max[3, Ceiling[window/4]],
      If[den > 10^-12, out[[i]] = num/den]],
    {i, window, n}];
  out
];

TBEquity[ind_Association, entryZ_?NumericQ, exitSigma_?NumericQ, initial_: 10000.] := Module[
  {z = ind["Z"], s = ind["Spread"], sig = ind["Sigma"], gross, n, eq, capital = initial,
   open = False, side, entry, entrySig, peak, ret, previous = Indeterminate, i, trades = 0},
  n = Length[z]; eq = ConstantArray[initial, n];
  Do[
    If[!NumericQ[z[[i]]], Continue[]];
    If[!open && NumericQ[previous] && previous >= entryZ && z[[i]] < entryZ,
      open = True; side = -1.; entry = s[[i]]; entrySig = sig[[i]]; peak = 0.; trades++];
    If[!open && NumericQ[previous] && previous <= -entryZ && z[[i]] > -entryZ,
      open = True; side = 1.; entry = s[[i]]; entrySig = sig[[i]]; peak = 0.; trades++];
    gross = ind["PY"][[i]] + Abs[ind["Beta"]] ind["PX"][[i]];
    If[open,
      ret = If[gross > 0, side (s[[i]] - entry)/gross, 0.];
      peak = Max[peak, ret]; eq[[i]] = capital (1 + ret);
      If[ret <= peak - exitSigma entrySig/gross || (side > 0 && z[[i]] >= 0) || (side < 0 && z[[i]] <= 0),
        capital = eq[[i]]; open = False],
      eq[[i]] = capital];
    previous = z[[i]],
    {i, n}];
  <|"Equity" -> eq, "Trades" -> trades|>
];

TBBoard[] := DynamicModule[
  {pairs = <||>, pair = "", y = "AA", x = "CENX", frequency = "日线", data = None,
   beta = 1., zWindow = 20, kWindow = 20, entryZ = 2., trailingSigma = .5, status = "读取 pair 后开始。"},
  Column[{
    Style["Trading batteries · Fixed-beta blackboard", 22, Bold],
    Style["原始 price 建模；不做首日归一化。图表只显示必要的数据与阈值。", 11],
    Row[{
      Button["读取 metapairs", pairs = TBPairs[];
        If[Length[pairs] > 0, pair = First@Keys[pairs]; status = "已读取 " <> ToString[Length[pairs]] <> " 个 active pair。", status = "无法读取 metapairs。"]],
      Spacer[8], Dynamic@If[Length[pairs] > 0, PopupMenu[Dynamic[pair], Keys[pairs], FieldSize -> 16], "尚未读取"],
      Spacer[14], "Y ", InputField[Dynamic[y], String, FieldSize -> 5], " X ", InputField[Dynamic[x], String, FieldSize -> 5],
      Spacer[8], PopupMenu[Dynamic[frequency], {"日线", "5 分钟"}],
      Spacer[8], Button["加载",
        If[KeyExistsQ[pairs, pair], {y, x} = pairs[pair]];
        data = TBRead[y, x, frequency];
        If[AssociationQ[data], beta = TBBeta[data["PY"], data["PX"]];
          status = y <> "/" <> x <> "：已加载 " <> ToString[Length[data["Times"]]] <> " 根共同 bar；beta = " <> ToString@NumberForm[beta, {8, 5}],
          status = "读取失败：请检查 ticker、数据表与 MySQL 连接。"]]
    }],
    Dynamic@Style[status, 11, Darker@Gray],
    Row[{"Fixed beta ", InputField[Dynamic[beta], Number, FieldSize -> 8],
      Spacer[10], "z window ", InputField[Dynamic[zWindow], Number, FieldSize -> 4],
      Spacer[10], "κ̂ window ", InputField[Dynamic[kWindow], Number, FieldSize -> 4],
      Spacer[10], "entry |z| ", InputField[Dynamic[entryZ], Number, FieldSize -> 4],
      Spacer[10], "trailing exit σ ", InputField[Dynamic[trailingSigma], Number, FieldSize -> 4]}],
    Dynamic@If[!AssociationQ[data], Style["加载共同价格后显示 Fixed beta spread、z-score、κ̂ 与 Equity。", Gray],
      Module[{ind, kap, equity, dates, line},
        If[Length[data["Times"]] < Max[zWindow, kWindow] + 2, Return[Style["当前窗口大于可用数据。", Darker@Red]]];
        ind = TBIndicators[data, beta, Max[2, Round[zWindow]]];
        kap = TBKappa[ind["Z"], Max[3, Round[kWindow]]];
        equity = TBEquity[ind, entryZ, trailingSigma];
        dates = ind["Times"];
        line[v_, label_] := DateListPlot[Transpose[{dates, v}], Joined -> True, Frame -> True,
          FrameLabel -> {"time", label}, PlotRange -> All, ImageSize -> 1050, AspectRatio -> .22,
          GridLines -> {None, Automatic}, GridLinesStyle -> LightGray];
        Column[{
          DateListPlot[{Transpose[{dates, ind["Spread"]}], Transpose[{dates, ind["Mean"]}],
            Transpose[{dates, ind["Mean"] + 2 ind["Sigma"]}], Transpose[{dates, ind["Mean"] - 2 ind["Sigma"]}]},
            Joined -> True, PlotStyle -> {Blue, Gray, Red, Red}, PlotLegends -> {"spread", "trailing mean", "+2σ", "-2σ"},
            Frame -> True, FrameLabel -> {"time", "Fixed-beta spread (raw price units)"}, PlotRange -> All,
            ImageSize -> 1050, AspectRatio -> .26, GridLines -> {None, Automatic}, GridLinesStyle -> LightGray],
          line[ind["Z"], "causal rolling z-score"],
          line[kap, "κ̂: contraction (<0), expansion (>0)"],
          line[equity["Equity"], "Equity: z re-entry; zero / trailing exit"],
          Style["Trades: " <> ToString[equity["Trades"]] <> "    Final equity: " <>
            ToString@NumberForm[Last[equity["Equity"]], {10, 2}], 11]
        }, Spacings -> .8]
      ]]
  }, Spacings -> 1.1]
];
