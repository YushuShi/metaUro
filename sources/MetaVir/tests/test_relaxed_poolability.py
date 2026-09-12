import copy
import math
import unittest
from statistics import NormalDist
from scripts import relaxed_poolability as policy
from scripts import dual_screening as ds

class RelaxedPoolabilityTests(unittest.TestCase):
    def base(self,**kw):
        row=dict(effect_type='RR',effect_size=2.,ci_lower=1.,ci_upper=4.,total_n=1000,cases=None,p_value=None,comparison_type=None,exposure_measurement_type=None,ratio_poolable=True,effect_family='ratio')
        row.update(kw);return row

    def test_optional_fields_do_not_block_moderate_or_require_r3(self):
        row=self.base(unit=None,scale=None,adjusted_model=None,gender=None)
        ds.add_wald_derivations(row)
        if hasattr(ds,'apply_poolability_policy'):ds.apply_poolability_policy(row)
        self.assertTrue(ds.is_poolable(row,{'grade':'Moderate'}))
        self.assertFalse(ds.needs_full_text(row,{'grade':'Moderate'}))
        self.assertTrue(ds.needs_full_text(row,{'grade':'Fair'}))
        self.assertFalse(ds.is_poolable(self.base(total_n=None),{'grade':'Good'}))

    def test_ci_only_ratio_is_geometric_midpoint_and_assumed_level(self):
        r=self.base(effect_size=None,ci_lower=.25,ci_upper=4.)
        policy.complete(r)
        self.assertAlmostEqual(r['effect_size'],1.)
        self.assertEqual(r['p_value'],1.)
        self.assertTrue(r['effect_size_imputed']);self.assertTrue(r['ci_level_assumed'])

    def test_log_ci_and_log_effect_are_converted_only_with_explicit_scale(self):
        r=self.base(effect_size=None,ci_lower=-2.,ci_upper=0.,ci_scale='log')
        policy.complete(r)
        self.assertAlmostEqual(r['effect_size'],math.exp(-1.))
        self.assertAlmostEqual(r['ci_lower'],math.exp(-2.))
        r=self.base(effect_size=-1.,ci_lower=-2.,ci_upper=0.,ci_scale='log',effect_scale='log')
        policy.complete(r);self.assertAlmostEqual(r['effect_size'],math.exp(-1.))

    def test_exact_p_wald_ci_matches_known_standard_error(self):
        z=NormalDist().inv_cdf(.975);effect=math.exp(z*.2)
        r=self.base(effect_size=effect,ci_lower=None,ci_upper=None,p_value=.05)
        policy.complete(r)
        self.assertAlmostEqual(r['ci_lower'],1.,places=12)
        self.assertAlmostEqual(r['ci_upper'],math.exp(2*z*.2),places=12)
        self.assertTrue(r['ci_imputed'])

    def test_p_bounds_and_noncoefficient_tests_are_never_exact(self):
        for kw in [dict(p_value='<0.05'),dict(p_value=.05,p_value_relation='less_than'),dict(p_value=.05,p_value_relation='<'),dict(p_value=.05,evidence={'p_value':{'excerpt':'P for trend = 0.05'}}),dict(p_value=.05,wald_inputs_same_estimate=False)]:
            with self.subTest(kw=kw):
                r=self.base(ci_lower=None,ci_upper=None,**kw);policy.complete(r);self.assertIsNone(r['ci_lower'])

    def test_non95_level_from_evidence_is_preserved_and_converted(self):
        r=self.base(evidence={'confidence_interval':{'excerpt':'RR 2.0 (99% CI 1.0–4.0)'}})
        policy.complete(r)
        self.assertEqual(r['ci_original_confidence_level'],99.)
        self.assertNotIn('ci_level_assumed',r)
        self.assertGreater(r['ci_lower'],1.)
        self.assertLess(r['ci_upper'],4.)

    def test_adjusted_aliases_and_same_contrast_preference(self):
        for alias,target in [('aRR','RR'),('ARR','RR'),('aOR','OR'),('AOR','OR'),('aHR','HR'),('AHR','HR')]:
            r=self.base(effect_type=alias);policy.complete(r)
            self.assertEqual(r['effect_type'],target);self.assertTrue(r['effect_is_adjusted'])
        raw=self.base(adjusted_model=None);adjusted=self.base(effect_is_adjusted=True)
        different=self.base(effect_is_adjusted=True,gender='Female')
        self.assertEqual(policy.prefer_adjusted([raw,adjusted,different]),[adjusted,different])
        self.assertIsNone(policy.canonical_ratio('standardized mean difference'))
        self.assertIsNone(policy.canonical_ratio('beta'))

    def test_missing_direction_imputed_but_explicit_inverse_preserved(self):
        r=self.base();policy.complete(r);self.assertTrue(r['direction_imputed'])
        r=self.base(comparison_type='Lowest versus highest',needs_inversion=True)
        policy.complete(r);self.assertFalse(r['direction_imputed']);self.assertTrue(r['needs_inversion'])
        r=self.base(comparison_type='Highest versus lowest; adjustment model unspecified')
        policy.complete(r);self.assertFalse(r['direction_imputed'])

    def test_completion_is_idempotent_and_does_not_fabricate_evidence(self):
        r=self.base();r['evidence']={};policy.complete(r);before=copy.deepcopy(r);policy.complete(r)
        self.assertEqual(r,before);self.assertEqual(r['evidence'],{})

    def test_numerical_edges_remain_nonpoolable(self):
        for updates in [dict(effect_size=1.,ci_lower=None,ci_upper=None,p_value=1.),dict(effect_size=2.,ci_lower=None,ci_upper=None,p_value=0.),dict(effect_size=None,ci_lower=0.,ci_upper=2.),dict(effect_size=None,ci_lower=2.,ci_upper=1.)]:
            r=self.base(**updates);policy.complete(r)
            self.assertTrue(r.get('effect_size') is None or r.get('ci_lower') is None or not (0<r.get('ci_lower',0)<r.get('ci_upper',0)))
        r=self.base(effect_size=2.,ci_lower=None,ci_upper=None,p_value=1e-250);policy.complete(r)
        self.assertTrue(math.isfinite(r['ci_lower']));self.assertLess(r['ci_lower'],2.);self.assertGreater(r['ci_upper'],2.)
if __name__=='__main__':unittest.main()
