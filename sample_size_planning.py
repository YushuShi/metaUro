"""Pure sample-size planning shared by request-local summary adapters."""
import math
from statistics import NormalDist

DEFAULTS = dict(alpha=0.05, power=0.8, allocation_ratio=1.0, sidedness='two', control_exposure_percent=50.0)

def planning(values):
    settings = dict(DEFAULTS)
    for name in ('alpha', 'power', 'allocation_ratio', 'control_exposure_percent'):
        raw = values.get(name, settings[name])
        try:
            number = float(raw)
        except (TypeError, ValueError):
            raise ValueError(f'{name} must be a finite number.') from None
        if isinstance(raw, bool) or not math.isfinite(number):
            raise ValueError(f'{name} must be a finite number.')
        settings[name] = number
    if not 0 < settings['alpha'] < 1: raise ValueError('alpha must be between 0 and 1.')
    if not 0.5 < settings['power'] < 1: raise ValueError('power must be greater than 0.5 and less than 1.')
    if settings['allocation_ratio'] <= 0: raise ValueError('allocation_ratio must be positive.')
    if not 0 < settings['control_exposure_percent'] < 100: raise ValueError('Control exposure must be greater than 0% and less than 100%.')
    sides = str(values.get('sidedness', 'two')).lower()
    if sides not in {'one', 'two', '1', '2'}: raise ValueError('sidedness must be one or two.')
    settings['sidedness'] = 'one' if sides in {'one','1'} else 'two'
    return settings

def total_sample_size(effect, baseline, design, settings):
    try:
        effect = float(effect)
        if not math.isfinite(effect) or effect <= 0: raise ValueError('No finite positive effect estimate is available.')
        sides = 1 if settings['sidedness'] == 'one' else 2
        za, zb = NormalDist().inv_cdf(1-settings['alpha']/sides), NormalDist().inv_cdf(settings['power'])
        ratio = settings['allocation_ratio']
        if design == 'retrospective':
            odds = effect if baseline is None else effect*(1-baseline)/(1-baseline*effect)
            if odds <= 0 or not math.isfinite(odds): raise ValueError('The effect implies an invalid odds ratio at this incidence.')
            q0 = settings['control_exposure_percent']/100
            q1 = 1/(1+((1-q0)/q0)/odds)
            diff = q1-q0
            if abs(diff) < 1e-12: raise ValueError('No finite sample size exists for an effect of 1.')
            avg = (q1+ratio*q0)/(1+ratio)
            reference = math.ceil((za*math.sqrt((1+1/ratio)*avg*(1-avg))+zb*math.sqrt(q1*(1-q1)+q0*(1-q0)/ratio))**2/diff**2)
        else:
            if baseline is None or not 0 < baseline < 1: raise ValueError('A baseline incidence greater than 0% and less than 100% is required.')
            p0, p1 = baseline, baseline*effect
            if not 0 <= p1 <= 1: raise ValueError('The effect implies a risk outside 0–100%.')
            diff = p1-p0
            if abs(diff) < 1e-12: raise ValueError('No finite sample size exists for an effect of 1.')
            avg = (p0+ratio*p1)/(1+ratio)
            reference = math.ceil((za*math.sqrt((1+1/ratio)*avg*(1-avg))+zb*math.sqrt(p0*(1-p0)+p1*(1-p1)/ratio))**2/diff**2)
        total = reference+math.ceil(ratio*reference)
        if total <= 0: raise ValueError('No finite sample size exists for this effect.')
        return total, None
    except (ValueError, OverflowError, ZeroDivisionError) as exc:
        return None, str(exc)
