"""Shared MetaVir exposure taxonomy for consistent forest/scatter colors.
Classification never changes an article's exposure measurement type.
"""
from pathlib import Path
import json
import re

CATEGORY_MEMBERS = {'Carotenoids': {'beta_carotene', 'lutein', 'zeaxanthin', 'lycopene'}, 'Vitamins A, C, D, E, K': {'vitamin_k1', 'vitamin_k3', 'vitamin_k', 'vitamin_a', 'vitamin_c', 'vitamin_k2', 'vitamin_d', 'vitamin_e'}, 'B Vitamins': {'vitamin_b1', 'vitamin_b7', 'pyridoxal_5_phosphate_plp', 'vitamin_b12', 'nicotinamide', 'biotin', 'vitamin_b6', 'vitamin_b5', 'folate', 'pantothenic_acid', 'vitamin_b9', 'niacin', 'folic_acid', 'vitamin_b2', 'vitamin_b3'}, 'Antioxidants': {'antioxidants', 'coenzyme_q10', 'n_acetylcysteine'}, 'Minerals & Trace Elements': {'selenium', 'magnesium', 'iodine', 'molybdenum', 'chromium', 'manganese', 'mineral_supplements', 'phosphorus', 'iron', 'cesium', 'fluoride', 'zinc', 'potassium', 'calcium', 'copper'}, 'Polyphenols & Flavonoids': {'green_tea', 'quercetin', 'resveratrol', 'tea'}, 'Fruits & Vegetables': {'avocado', 'sesame', 'grape', 'olive', 'garlic', 'tomato', 'pepper_capsicum', 'legumes', 'grapefruit', 'mushrooms', 'papaya'}, 'Fermented Foods & Probiotics': {'probiotics', 'kefir', 'natto', 'miso', 'pickled_vegetables', 'sauerkraut', 'skyr', 'yogurt', 'lactobacillus', 'kimchi', 'fermented_foods'}, 'Fatty Acids & Lipids': {'capric_acid', 'caprylic_acid', 'docosapentaenoic_acid_omega_6', 'hemp_seeds', 'alpha_linolenic_acid', 'omega_7_fatty_acids', 'stearic_acid', 'myristic_acid', 'docosapentaenoic_acid_omega_3', 'eicosapentaenoic_acid', 'polyunsaturated_fat_pufa', 'arachidonic_acid', 'omega_6_fatty_acids', 'fish_oil', 'linoleic_acid', 'omega_3_fatty_acids', 'docosahexaenoic_acid', 'conjugated_linoleic_acid', 'omega_9_fatty_acids', 'chia_seeds', 'palmitic_acid', 'oleic_acid', 'adrenic_acid', 'butyric_acid', 'monounsaturated_fat_mufa', 'trans_fat', 'cis_vaccenic_acid', 'saturated_fat', 'palmitoleic_acid', 'phosphatidylcholine', 'cod_liver_oil', 'lauric_acid', 'alpha_lipoic_acid'}, 'Phytoestrogens': {'flaxseed', 'genistein', 'flax', 'soy', 'red_clover'}, 'Herbal & Botanical': {'fo_ti', 'ginkgo_biloba', 'sage', 'blue_green_algae', 'ginkgo', 'black_cohosh', 'cannabidiol', 'saw_palmetto', 'ginseng'}, 'Metabolites & Amino Acids': {'leucine', 'isoleucine', 'protein_intake', 'l_tryptophan', 'l_tyrosine', 'l_valine', 'inositol', 'dietary_protein_intake', 'betaine', 'bcaas', 'carnitine', 'chondroitin', 'citrulline', 'glutamine', 'glucosamine', 'nitric_oxide', 'choline', 'plant_protein_intake', 'pyruvate', 'animal_protein_intake', 'egg_protein', 'creatine', 'glycine'}, 'Hormones & Endogenous': {'melatonin', 'dehydroepiandrosterone'}, 'Diet': {'anti_inflammatory_diet', 'early_time_restricted_eating', 'intermittent_fasting', 'mediterranean_diet', 'alternate_day_fasting', 'low_fat_diet', 'mind_diet', 'western_pattern_diet', 'vegetarian_diet', 'planetary_health_diet', 'time_restricted_eating', 'high_fiber_diet', 'low_carbohydrate_diet', 'ketogenic_diet', 'vegan_diet', 'dietary_plant_diversity', 'dash_diet', 'plant_based_diet'}}

CATEGORY_COLORS = {'Carotenoids': '#E55300', 'Vitamins A, C, D, E, K': '#C79000', 'B Vitamins': '#0057B8', 'Antioxidants': '#C23B79', 'Minerals & Trace Elements': '#007C7C', 'Polyphenols & Flavonoids': '#6A0DAD', 'Fruits & Vegetables': '#2E7D32', 'Fermented Foods & Probiotics': '#BC6C25', 'Fatty Acids & Lipids': '#B5001F', 'Phytoestrogens': '#7B4A00', 'Herbal & Botanical': '#00695C', 'Metabolites & Amino Acids': '#880E4F', 'Hormones & Endogenous': '#4527A0', 'Other': '#37474F', 'Diet': '#A437A0'}


def key(value):
    return re.sub(r'[^a-z0-9]+', '_', str(value).casefold()).strip('_')


def exposure_category(slug, repo_root=None):
    slug = key(slug)
    if slug in CATEGORY_MEMBERS['Diet'] or re.search(r'(?:^|_)(?:diet|dietary_pattern|eating|fasting)(?:_|$)', slug):
        return 'Diet'
    if re.search(r'(?:^|_)(?:fatty_acids?|omega_[3679]|saturated_fat|unsaturated_fat|trans_fat)(?:_|$)', slug):
        return 'Fatty Acids & Lipids'
    if repo_root is not None:
        path=Path(repo_root)/'data/exposure_hierarchy.json'
        hierarchy=json.loads(path.read_text()) if path.exists() else {}
        graph={key(child):[key(p) for p in parents] for child,parents in hierarchy.items()}
        seen=set();pending=list(graph.get(slug,[]))
        while pending:
            parent=pending.pop()
            if parent in seen:continue
            seen.add(parent);pending.extend(graph.get(parent,[]))
        if seen & CATEGORY_MEMBERS['Fatty Acids & Lipids']:return 'Fatty Acids & Lipids'
        if seen & CATEGORY_MEMBERS['B Vitamins']:return 'B Vitamins'
        if seen & CATEGORY_MEMBERS['Diet']:return 'Diet'
    return next((category for category,members in CATEGORY_MEMBERS.items() if slug in members),'Other')
