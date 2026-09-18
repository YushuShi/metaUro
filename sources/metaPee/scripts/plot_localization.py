"""Strict localization helpers for cross-exposure summary figures.

The summary builders intentionally fail when a visible plot label is missing.
That prevents a translated PDF from silently mixing English with the selected
language.  MetaPee reuses its site-wide translation catalogue for exposure
names; MetaGut keeps its plot-specific exposure labels here because its browser
catalogue has a different, smaller shape.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Iterable

from matplotlib import font_manager


SITE_LANGUAGES = {
    "metagut": ("en", "zh-CN", "es", "fr"),
    "metapee": ("en", "zh-CN", "zh-TW", "nl", "ko"),
}


def _parse_table(table: str) -> dict[str, dict[str, str]]:
    lines = [line for line in table.strip().splitlines() if line.strip()]
    header = lines[0].split("\t")
    parsed: dict[str, dict[str, str]] = {}
    for line in lines[1:]:
        fields = line.split("\t")
        if len(fields) != len(header):
            raise ValueError(f"Malformed plot-localization row: {line!r}")
        parsed[fields[0]] = dict(zip(header[1:], fields[1:]))
    return parsed


PLOT_STRINGS = _parse_table(
    """
key\ten\tzh-CN\tes\tfr\tzh-TW\tnl\tko
exposure\tExposure\t暴露因素\tExposición\tExposition\t暴露因子\tBlootstelling\t노출 요인
number_of_estimates\tEstimates\t估计值数\tEstimaciones\tEstimations\t估計值數\tSchattingen\t추정치 수
sample_size\tN\t样本量\tN\tN\t樣本數\tN\t표본 크기
cases\tCases\t病例数\tCasos\tCas\t病例數\tGevallen\t증례 수
pooled_rr_ci\tPooled RR (95% CI)\t合并 RR（95% CI）\tRR combinado (IC del 95%)\tRR combiné (IC à 95 %)\t合併 RR（95% CI）\tGepoolde RR (95%-BI)\t통합 RR (95% CI)
effect_size\tEffect size\t效应量\tTamaño del efecto\tTaille de l'effet\t效應量\tEffectgrootte\t효과 크기
pooled_rr\tPooled RR\t合并 RR\tRR combinado\tRR combiné\t合併 RR\tGepoolde RR\t통합 RR
exposure_group\tExposure group\t暴露类别\tGrupo de exposición\tGroupe d'exposition\t暴露類別\tBlootstellingsgroep\t노출 요인군
protective_title\tExposures negatively associated with {disease} risk\t与{disease}风险呈负相关的暴露因素\tExposiciones asociadas negativamente con el riesgo de {disease}\tExpositions associées négativement au risque de {disease}\t與{disease}風險呈負相關的暴露因子\tBlootstellingen negatief geassocieerd met het risico op {disease}\t{disease} 위험과 음의 관련성을 보인 노출 요인
harmful_title\tExposures positively associated with {disease} risk\t与{disease}风险呈正相关的暴露因素\tExposiciones asociadas positivamente con el riesgo de {disease}\tExpositions associées positivement au risque de {disease}\t與{disease}風險呈正相關的暴露因子\tBlootstellingen positief geassocieerd met het risico op {disease}\t{disease} 위험과 양의 관련성을 보인 노출 요인
dietary_subtitle\tMeta-analysis of dietary-intake studies\t膳食摄入研究的荟萃分析\tMetanálisis de estudios de ingesta dietética\tMéta-analyse des études sur l'apport alimentaire\t膳食攝取研究的統合分析\tMeta-analyse van onderzoeken naar voedingsinname\t식이 섭취 연구의 메타분석
inverse_caption\tRR < 1 = negatively associated with {disease} risk.\tRR < 1 = 与{disease}风险呈负相关。\tRR < 1 = asociación negativa con el riesgo de {disease}.\tRR < 1 = association négative avec le risque de {disease}.\tRR < 1 = 與{disease}風險呈負相關。\tRR < 1 = negatief geassocieerd met het risico op {disease}.\tRR < 1 = {disease} 위험과 음의 관련성.
positive_caption\tRR > 1 = positively associated with {disease} risk.\tRR > 1 = 与{disease}风险呈正相关。\tRR > 1 = asociación positiva con el riesgo de {disease}.\tRR > 1 = association positive avec le risque de {disease}.\tRR > 1 = 與{disease}風險呈正相關。\tRR > 1 = positief geassocieerd met het risico op {disease}.\tRR > 1 = {disease} 위험과 양의 관련성.
pooled_marker_caption\t◆ pooled RR (size reflects number of estimates)\t◆ 合并 RR（大小反映估计值数量）\t◆ RR combinado (el tamaño refleja el número de estimaciones)\t◆ RR combiné (la taille reflète le nombre d'estimations)\t◆ 合併 RR（大小反映估計值數量）\t◆ gepoolde RR (grootte weerspiegelt aantal schattingen)\t◆ 통합 RR (크기는 추정치 수를 반영)
significant_caption\t◇ red outline = statistically significant (95% CI excludes 1.0)\t◇ 红色轮廓 = 具有统计学意义（95% CI 不包含 1.0）\t◇ contorno rojo = estadísticamente significativo (el IC del 95% excluye 1,0)\t◇ contour rouge = statistiquement significatif (l'IC à 95 % exclut 1,0)\t◇ 紅色外框 = 具統計顯著性（95% CI 不含 1.0）\t◇ rode rand = statistisch significant (95%-BI sluit 1,0 uit)\t◇ 빨간 윤곽선 = 통계적으로 유의함 (95% CI에 1.0 미포함)
nonsignificant_caption\t◆ faded = not statistically significant\t◆ 淡色 = 无统计学意义\t◆ atenuado = sin significación estadística\t◆ atténué = non statistiquement significatif\t◆ 淡色 = 無統計顯著性\t◆ vervaagd = niet statistisch significant\t◆ 흐린 표시 = 통계적으로 유의하지 않음
prediction_interval_caption\tdashed line = 95% prediction interval\t虚线 = 95% 预测区间\tlínea discontinua = intervalo de predicción del 95%\tligne en tirets = intervalle de prédiction à 95 %\t虛線 = 95% 預測區間\tstreepjeslijn = 95%-voorspellingsinterval\t점선 = 95% 예측구간
estimate_overlap_caption\tN and case totals sum estimates and may overlap across strata\tN 和病例总数按估计值求和，不同分层间可能重叠\tLos totales de N y casos suman estimaciones y pueden solaparse entre estratos\tLes totaux de N et de cas additionnent les estimations et peuvent se chevaucher entre strates\tN 和病例總數按估計值加總，不同分層間可能重疊\tN- en gevallentotalen tellen schattingen op en kunnen tussen strata overlappen\tN과 증례 합계는 추정치별 합산이며 층 간 중복될 수 있음
interval_truncation_caption\tarrowhead = interval continues beyond the axis\t箭头 = 区间延伸至坐标轴范围之外\tpunta de flecha = el intervalo continúa fuera del eje\tpointe de flèche = l'intervalle se prolonge au-delà de l'axe\t箭頭 = 區間延伸至座標軸範圍之外\tpijlpunt = interval loopt door buiten de as\t화살촉 = 구간이 축 범위 밖으로 이어짐
heterogeneity_y\tHeterogeneity, I² (%)\t异质性，I²（%）\tHeterogeneidad, I² (%)\tHétérogénéité, I² (%)\t異質性，I²（%）\tHeterogeniteit, I² (%)\t이질성, I² (%)
effect_heterogeneity_title\tEffect size vs heterogeneity - {disease}\t效应量与异质性 - {disease}\tTamaño del efecto frente a la heterogeneidad - {disease}\tTaille de l'effet en fonction de l'hétérogénéité - {disease}\t效應量與異質性 - {disease}\tEffectgrootte versus heterogeniteit - {disease}\t효과 크기와 이질성 - {disease}
effect_heterogeneity_x\tEffect size (pooled RR)\t效应量（合并 RR）\tTamaño del efecto (RR combinado)\tTaille de l'effet (RR combiné)\t效應量（合併 RR）\tEffectgrootte (gepoolde RR)\t효과 크기 (통합 RR)
egger_heterogeneity_title\tEgger's test log(p-value) vs heterogeneity - {disease}\tEgger 检验 log(p 值) 与异质性 - {disease}\tLog(valor p) de la prueba de Egger frente a la heterogeneidad - {disease}\tLog(valeur p) du test d'Egger en fonction de l'hétérogénéité - {disease}\tEgger 檢定 log(p 值) 與異質性 - {disease}\tLog(p-waarde) van Eggers toets versus heterogeniteit - {disease}\tEgger 검정 log(p값)과 이질성 - {disease}
egger_x\tlog(Egger's p-value)\tlog(Egger 检验 p 值)\tlog(valor p de la prueba de Egger)\tlog(valeur p du test d'Egger)\tlog(Egger 檢定 p 值)\tlog(p-waarde van Eggers toets)\tlog(Egger 검정 p값)
effect_diagnostic_caption\tPoint size reflects the number of estimates; label colors identify exposure groups; the dashed line marks RR = 1.\t点大小反映估计值数量；标签颜色表示暴露类别；虚线标示 RR = 1。\tEl tamaño del punto refleja el número de estimaciones; los colores de las etiquetas identifican los grupos de exposición; la línea discontinua marca RR = 1.\tLa taille du point reflète le nombre d'estimations ; la couleur des étiquettes indique les groupes d'exposition ; la ligne en tirets marque RR = 1.\t點大小反映估計值數量；標籤顏色表示暴露類別；虛線標示 RR = 1。\tDe puntgrootte weerspiegelt het aantal schattingen; labelkleuren geven blootstellingsgroepen aan; de streepjeslijn markeert RR = 1.\t점 크기는 추정치 수를 반영하며, 라벨 색상은 노출 요인군을 나타내고, 점선은 RR = 1을 표시합니다.
egger_diagnostic_caption\tPoint size reflects the number of estimates; label colors identify exposure groups; the dashed line marks Egger's p = 0.05.\t点大小反映估计值数量；标签颜色表示暴露类别；虚线标示 Egger 检验 p = 0.05。\tEl tamaño del punto refleja el número de estimaciones; los colores de las etiquetas identifican los grupos de exposición; la línea discontinua marca p de Egger = 0,05.\tLa taille du point reflète le nombre d'estimations ; la couleur des étiquettes indique les groupes d'exposition ; la ligne en tirets marque p d'Egger = 0,05.\t點大小反映估計值數量；標籤顏色表示暴露類別；虛線標示 Egger 檢定 p = 0.05。\tDe puntgrootte weerspiegelt het aantal schattingen; labelkleuren geven blootstellingsgroepen aan; de streepjeslijn markeert Eggers p = 0,05.\t점 크기는 추정치 수를 반영하며, 라벨 색상은 노출 요인군을 나타내고, 점선은 Egger 검정 p = 0.05를 표시합니다.
"""
)


CATEGORY_LABELS = _parse_table(
    """
key\ten\tzh-CN\tes\tfr\tzh-TW\tnl\tko
Carotenoids\tCarotenoids\t类胡萝卜素\tCarotenoides\tCaroténoïdes\t類胡蘿蔔素\tCarotenoïden\t카로티노이드
Vitamins A, C, D, E, K\tVitamins A, C, D, E, K\t维生素 A、C、D、E、K\tVitaminas A, C, D, E y K\tVitamines A, C, D, E et K\t維生素 A、C、D、E、K\tVitaminen A, C, D, E en K\t비타민 A, C, D, E, K
Diet\tDiet\t饮食模式\tPatrones alimentarios\tRégimes alimentaires\t飲食模式\tVoedingspatronen\t식사 패턴
B Vitamins\tB Vitamins\tB 族维生素\tVitaminas B\tVitamines B\tB 群維生素\tB-vitaminen\t비타민 B군
Antioxidants\tAntioxidants\t抗氧化剂\tAntioxidantes\tAntioxydants\t抗氧化劑\tAntioxidanten\t항산화제
Minerals & Trace Elements\tMinerals & Trace Elements\t矿物质与微量元素\tMinerales y oligoelementos\tMinéraux et oligo-éléments\t礦物質與微量元素\tMineralen en sporenelementen\t무기질 및 미량 원소
Polyphenols & Flavonoids\tPolyphenols & Flavonoids\t多酚与黄酮类\tPolifenoles y flavonoides\tPolyphénols et flavonoïdes\t多酚與類黃酮\tPolyfenolen en flavonoïden\t폴리페놀 및 플라보노이드
Fruits & Vegetables\tFruits & Vegetables\t水果与蔬菜\tFrutas y verduras\tFruits et légumes\t水果與蔬菜\tFruit en groenten\t과일 및 채소
Fermented Foods & Probiotics\tFermented Foods & Probiotics\t发酵食品与益生菌\tAlimentos fermentados y probióticos\tAliments fermentés et probiotiques\t發酵食品與益生菌\tGefermenteerde voeding en probiotica\t발효 식품 및 프로바이오틱스
Fatty Acids & Lipids\tFatty Acids & Lipids\t脂肪酸与脂质\tÁcidos grasos y lípidos\tAcides gras et lipides\t脂肪酸與脂質\tVetzuren en lipiden\t지방산 및 지질
Phytoestrogens\tPhytoestrogens\t植物雌激素\tFitoestrógenos\tPhytoestrogènes\t植物雌激素\tFyto-oestrogenen\t식물성 에스트로겐
Herbal & Botanical\tHerbal & Botanical\t草本与植物制品\tHierbas y productos botánicos\tPlantes et produits botaniques\t草本與植物製品\tKruiden en botanische producten\t허브 및 식물성 제품
Metabolites & Amino Acids\tMetabolites & Amino Acids\t代谢物与氨基酸\tMetabolitos y aminoácidos\tMétabolites et acides aminés\t代謝物與胺基酸\tMetabolieten en aminozuren\t대사산물 및 아미노산
Hormones & Endogenous\tHormones & Endogenous\t激素与内源性物质\tHormonas y sustancias endógenas\tHormones et substances endogènes\t荷爾蒙與內源性物質\tHormonen en endogene stoffen\t호르몬 및 내인성 물질
Other\tOther\t其他\tOtros\tAutres\t其他\tOverig\t기타
"""
)


GUT_EXPOSURE_LABELS = _parse_table(
    """
key\tzh-CN\tes\tfr
alcohol\t酒精\tAlcohol\tAlcool
antioxidants\t抗氧化剂\tAntioxidantes\tAntioxydants
bcaas\t支链氨基酸（BCAA）\tAminoácidos de cadena ramificada (BCAA)\tAcides aminés à chaîne ramifiée (BCAA)
beta_carotene\tβ-胡萝卜素\tBetacaroteno\tBêta-carotène
betaine\t甜菜碱\tBetaína\tBétaïne
blue_green_algae\t蓝绿藻\tAlgas verdeazuladas\tAlgues bleu-vert
caffeine\t咖啡因\tCafeína\tCaféine
calcium\t钙\tCalcio\tCalcium
carnitine\t肉碱\tCarnitina\tCarnitine
chocolate\t巧克力\tChocolate\tChocolat
choline\t胆碱\tColina\tCholine
chondroitin\t软骨素\tCondroitina\tChondroïtine
chromium\t铬\tCromo\tChrome
copper\t铜\tCobre\tCuivre
dairy\t乳制品\tProductos lácteos\tProduits laitiers
dehydroepiandrosterone\t脱氢表雄酮（DHEA）\tDehidroepiandrosterona (DHEA)\tDéhydroépiandrostérone (DHEA)
eggs\t鸡蛋\tHuevos\tŒufs
fermented_foods\t发酵食品\tAlimentos fermentados\tAliments fermentés
fish_oil\t鱼油\tAceite de pescado\tHuile de poisson
fluoride\t氟化物\tFluoruro\tFluorure
folic_acid\t叶酸\tÁcido fólico\tAcide folique
garlic\t大蒜\tAjo\tAil
glucosamine\t氨基葡萄糖\tGlucosamina\tGlucosamine
glutamine\t谷氨酰胺\tGlutamina\tGlutamine
green_tea\t绿茶\tTé verde\tThé vert
iodine\t碘\tYodo\tIode
iron\t铁\tHierro\tFer
isoleucine\t异亮氨酸\tIsoleucina\tIsoleucine
kimchi\t韩式泡菜\tKimchi\tKimchi
l_tryptophan\tL-色氨酸\tL-triptófano\tL-tryptophane
legumes\t豆类\tLegumbres\tLégumineuses
leucine\t亮氨酸\tLeucina\tLeucine
lycopene\t番茄红素\tLicopeno\tLycopène
magnesium\t镁\tMagnesio\tMagnésium
manganese\t锰\tManganeso\tManganèse
mediterranean_diet\t地中海饮食\tDieta mediterránea\tRégime méditerranéen
melatonin\t褪黑素\tMelatonina\tMélatonine
mineral_supplements\t矿物质补充剂\tSuplementos minerales\tSuppléments minéraux
miso\t味噌\tMiso\tMiso
molybdenum\t钼\tMolibdeno\tMolybdène
multivitamin\t复合维生素\tMultivitamínicos\tMultivitamines
nitric_oxide\t一氧化氮\tÓxido nítrico\tOxyde nitrique
olive\t橄榄\tAceituna\tOlive
omega_3_fatty_acids\tOmega-3 脂肪酸\tÁcidos grasos omega-3\tAcides gras oméga-3
omega_6_fatty_acids\tOmega-6 脂肪酸\tÁcidos grasos omega-6\tAcides gras oméga-6
pepper_capsicum\t辣椒（Capsicum）\tPimiento (Capsicum)\tPiment (Capsicum)
phosphorus\t磷\tFósforo\tPhosphore
pickled_vegetables\t腌制蔬菜\tVerduras encurtidas\tLégumes marinés
potassium\t钾\tPotasio\tPotassium
probiotics\t益生菌\tProbióticos\tProbiotiques
pyruvate\t丙酮酸\tPiruvato\tPyruvate
quercetin\t槲皮素\tQuercetina\tQuercétine
red_meat\t红肉\tCarne roja\tViande rouge
resveratrol\t白藜芦醇\tResveratrol\tResvératrol
selenium\t硒\tSelenio\tSélénium
soy\t大豆\tSoja\tSoja
tea\t茶\tTé\tThé
tomato\t番茄\tTomate\tTomate
vitamin_a\t维生素 A\tVitamina A\tVitamine A
vitamin_b1\t维生素 B1\tVitamina B1\tVitamine B1
vitamin_b12\t维生素 B12\tVitamina B12\tVitamine B12
vitamin_b2\t维生素 B2\tVitamina B2\tVitamine B2
vitamin_b6\t维生素 B6\tVitamina B6\tVitamine B6
vitamin_c\t维生素 C\tVitamina C\tVitamine C
vitamin_d\t维生素 D\tVitamina D\tVitamine D
vitamin_e\t维生素 E\tVitamina E\tVitamine E
yogurt\t酸奶\tYogur\tYaourt
zinc\t锌\tZinc\tZinc
"""
)


DISEASE_LABELS = _parse_table(
    """
key\tzh-CN\tes\tfr\tzh-TW\tnl\tko
Colon adenocarcinoma\t结肠腺癌\tadenocarcinoma de colon\tadénocarcinome du côlon\t結腸腺癌\tadenocarcinoom van de dikke darm\t결장 선암
Colorectal cancer\t结直肠癌\tcáncer colorrectal\tcancer colorectal\t大腸直腸癌\tdarmkanker\t대장암
Esophagus adenocarcinoma\t食管腺癌\tadenocarcinoma de esófago\tadénocarcinome de l'œsophage\t食道腺癌\tadenocarcinoom van de slokdarm\t식도 선암
Esophagus cancer\t食管癌\tcáncer de esófago\tcancer de l'œsophage\t食道癌\tslokdarmkanker\t식도암
Esophagus squamous cell carcinoma\t食管鳞状细胞癌\tcarcinoma escamocelular de esófago\tcarcinome épidermoïde de l'œsophage\t食道鱗狀細胞癌\tplaveiselcelcarcinoom van de slokdarm\t식도 편평세포암
Intrahepatic cholangiocarcinoma\t肝内胆管癌\tcolangiocarcinoma intrahepático\tcholangiocarcinome intrahépatique\t肝內膽管癌\tintrahepatisch cholangiocarcinoom\t간내 담관암
Liver and intrahepatic bile duct cancer\t肝和肝内胆管癌\tcáncer de hígado y vías biliares intrahepáticas\tcancer du foie et des voies biliaires intrahépatiques\t肝臟及肝內膽管癌\tlever- en intrahepatische galwegkanker\t간 및 간내 담관암
Liver hepatocellular carcinoma\t肝细胞癌\tcarcinoma hepatocelular\tcarcinome hépatocellulaire\t肝細胞癌\thepatocellulair carcinoom\t간세포암
Pancreatic cancer\t胰腺癌\tcáncer de páncreas\tcancer du pancréas\t胰臟癌\talvleesklierkanker\t췌장암
Pancreatic ductal adenocarcinoma\t胰腺导管腺癌\tadenocarcinoma ductal de páncreas\tadénocarcinome canalaire du pancréas\t胰臟導管腺癌\tductaal adenocarcinoom van de alvleesklier\t췌관 선암
Pancreatic neuroendocrine tumor (PanNET)\t胰腺神经内分泌肿瘤（PanNET）\ttumor neuroendocrino pancreático (PanNET)\ttumeur neuroendocrine pancréatique (PanNET)\t胰臟神經內分泌腫瘤（PanNET）\tpancreatische neuro-endocriene tumor (PanNET)\t췌장 신경내분비종양 (PanNET)
Small intestine adenocarcinoma\t小肠腺癌\tadenocarcinoma de intestino delgado\tadénocarcinome de l'intestin grêle\t小腸腺癌\tadenocarcinoom van de dunne darm\t소장 선암
Small intestine cancer\t小肠癌\tcáncer de intestino delgado\tcancer de l'intestin grêle\t小腸癌\tdunnedarmkanker\t소장암
Stomach adenocarcinoma\t胃腺癌\tadenocarcinoma gástrico\tadénocarcinome gastrique\t胃腺癌\tadenocarcinoom van de maag\t위 선암
Stomach cancer\t胃癌\tcáncer de estómago\tcancer de l'estomac\t胃癌\tmaagkanker\t위암
Bladder cancer\t膀胱癌\tcáncer de vejiga\tcancer de la vessie\t膀胱癌\tblaaskanker\t방광암
Urothelial carcinoma of the bladder\t膀胱尿路上皮癌\tcarcinoma urotelial de vejiga\tcarcinome urothélial de la vessie\t膀胱尿路上皮癌\turotheelcarcinoom van de blaas\t방광 요로상피암
Squamous cell carcinoma of the bladder\t膀胱鳞状细胞癌\tcarcinoma escamocelular de vejiga\tcarcinome épidermoïde de la vessie\t膀胱鱗狀細胞癌\tplaveiselcelcarcinoom van de blaas\t방광 편평세포암
Adenocarcinoma of the bladder\t膀胱腺癌\tadenocarcinoma de vejiga\tadénocarcinome de la vessie\t膀胱腺癌\tadenocarcinoom van de blaas\t방광 선암
Small cell carcinoma of the bladder\t膀胱小细胞癌\tcarcinoma microcítico de vejiga\tcarcinome à petites cellules de la vessie\t膀胱小細胞癌\tkleincellig carcinoom van de blaas\t방광 소세포암
Kidney and renal pelvis cancer\t肾和肾盂癌\tcáncer de riñón y pelvis renal\tcancer du rein et du bassinet\t腎臟及腎盂癌\tnier- en nierbekkenkanker\t신장 및 신우암
Renal cell carcinoma\t肾细胞癌\tcarcinoma de células renales\tcarcinome à cellules rénales\t腎細胞癌\tniercelcarcinoom\t신세포암
Clear cell renal cell carcinoma\t透明细胞肾细胞癌\tcarcinoma renal de células claras\tcarcinome rénal à cellules claires\t透明細胞腎細胞癌\theldercellig niercelcarcinoom\t투명세포 신세포암
Papillary renal cell carcinoma\t乳头状肾细胞癌\tcarcinoma renal papilar\tcarcinome rénal papillaire\t乳頭狀腎細胞癌\tpapillair niercelcarcinoom\t유두상 신세포암
Chromophobe renal cell carcinoma\t嫌色素性肾细胞癌\tcarcinoma renal cromófobo\tcarcinome rénal chromophobe\t嫌色素性腎細胞癌\tchromofobe niercelcarcinoom\t크로모포브 신세포암
Urothelial carcinoma of the renal pelvis\t肾盂尿路上皮癌\tcarcinoma urotelial de la pelvis renal\tcarcinome urothélial du bassinet\t腎盂尿路上皮癌\turotheelcarcinoom van het nierbekken\t신우 요로상피암
Renal sarcoma\t肾肉瘤\tsarcoma renal\tsarcome rénal\t腎肉瘤\tniersarcoom\t신장 육종
"""
)


FONT_CANDIDATES = {
    "zh-CN": (
        "Noto Sans CJK SC", "Source Han Sans SC", "PingFang SC",
        "Hiragino Sans GB", "Microsoft YaHei", "Arial Unicode MS", "PingFang HK",
    ),
    "zh-TW": (
        "Noto Sans CJK TC", "Source Han Sans TC", "PingFang TC",
        "PingFang HK", "Microsoft JhengHei", "Arial Unicode MS", "Heiti TC",
    ),
    "ko": (
        "Noto Sans CJK KR", "Noto Sans KR", "Source Han Sans K",
        "Apple SD Gothic Neo", "Malgun Gothic", "Arial Unicode MS",
    ),
}


def normalize_key(value: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]", "_", str(value).lower())).strip("_")


def site_key(repo_root: Path) -> str:
    if (repo_root / "digestive_cancer_types.csv").is_file():
        return "metagut"
    if (repo_root / "bladder_kidney_cancer_lifetime_incidence_aliases.csv").is_file():
        return "metapee"
    raise ValueError(f"Cannot infer plot-localization site from {repo_root}")


def supported_languages(repo_root: Path) -> tuple[str, ...]:
    return SITE_LANGUAGES[site_key(repo_root)]


def text(key: str, locale: str = "en", **values: str) -> str:
    try:
        template = PLOT_STRINGS[key][locale]
    except KeyError as exc:
        raise KeyError(f"Missing plot string {key!r} for locale {locale!r}") from exc
    return template.format(**values)


def category_label(category: str, locale: str = "en") -> str:
    try:
        return CATEGORY_LABELS[category][locale]
    except KeyError as exc:
        raise KeyError(f"Missing category translation {category!r} for locale {locale!r}") from exc


def display_exposure(slug: str) -> str:
    label = slug.replace("_", " ").title()
    replacements = {
        "Plp": "PLP", "Mufa": "MUFA", "Pufa": "PUFA", "Dash Diet": "DASH Diet",
        "Bcaas": "BCAAs",
        "Coenzyme Q10": "Coenzyme Q10",
        "L Tryptophan": "L-Tryptophan",
        "Omega 3": "Omega-3",
        "Omega 6": "Omega-6",
        "Omega 9": "Omega-9",
        "Fo Ti": "Fo-ti",
        "Dna": "DNA",
    }
    for old, new in replacements.items():
        label = label.replace(old, new)
    return label


@lru_cache(maxsize=None)
def _site_catalog(repo_root: str) -> dict[str, dict[str, str]]:
    path = Path(repo_root) / "static" / "i18n-translations.json"
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {normalize_key(source): translations for source, translations in payload.items()}


def exposure_label(repo_root: Path, slug: str, locale: str = "en") -> str:
    if locale == "en":
        return display_exposure(slug)
    site = site_key(repo_root)
    if site == "metagut":
        try:
            return GUT_EXPOSURE_LABELS[slug][locale]
        except KeyError as exc:
            raise KeyError(f"Missing MetaGut exposure translation {slug!r} for {locale!r}") from exc
    catalog = _site_catalog(str(repo_root.resolve()))
    try:
        return str(catalog[normalize_key(slug)][locale])
    except KeyError as exc:
        raise KeyError(f"Missing MetaPee exposure translation {slug!r} for {locale!r}") from exc


def disease_label(disease: str, locale: str = "en") -> str:
    if locale == "en":
        return disease[:1].lower() + disease[1:]
    try:
        return DISEASE_LABELS[disease][locale]
    except KeyError as exc:
        raise KeyError(f"Missing disease translation {disease!r} for {locale!r}") from exc


@lru_cache(maxsize=None)
def font_family(locale: str) -> str:
    if locale not in FONT_CANDIDATES:
        return "DejaVu Sans"
    available = {font.name for font in font_manager.fontManager.ttflist}
    for candidate in FONT_CANDIDATES[locale]:
        if candidate in available:
            return candidate
    raise RuntimeError(
        f"No CJK-capable font is installed for {locale}; install Noto Sans CJK "
        "before generating localized PDFs."
    )


def validate_localizations(
    repo_root: Path,
    exposures: Iterable[str],
    diseases: Iterable[str],
    categories: Iterable[str],
    locales: Iterable[str] | None = None,
) -> None:
    """Fail before rendering if any visible localized value is unavailable."""
    selected_locales = tuple(locales or supported_languages(repo_root))
    unsupported = set(selected_locales).difference(supported_languages(repo_root))
    if unsupported:
        raise ValueError(f"Unsupported localization validation languages: {sorted(unsupported)}")
    for locale in selected_locales:
        for key in PLOT_STRINGS:
            text(key, locale, disease="test disease")
        for category in categories:
            category_label(category, locale)
        for disease in diseases:
            disease_label(disease, locale)
        for exposure in exposures:
            exposure_label(repo_root, exposure, locale)

# Shared diagnostic vocabulary follows the requested base-10 scale.
from scripts.diagnostic_style import STYLE_STRINGS
PLOT_STRINGS.update(STYLE_STRINGS)
