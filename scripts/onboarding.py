#!/usr/bin/env python3
"""Local install wizard and validated paper-run settings. No broker requests."""
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / 'desk/runtime/.state/setup/profile.json'
LIQUIDITY = {
    'high': dict(max_spread_bps='5', min_bar_dollars='100000'),
    'balanced': dict(max_spread_bps='15', min_bar_dollars='25000'),
    'flexible': dict(max_spread_bps='30', min_bar_dollars='5000'),
}
FAMILIES = ['sma_cross_long', 'momentum_long', 'mean_reversion_long', 'breakout_long', 'upstream_ema']


def amount(value, minimum='0', maximum='1000000000'):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]+(?:\.[0-9]{1,2})?', value):
        raise ValueError('Enter a dollar amount with at most two decimal places')
    number = Decimal(value)
    if not Decimal(minimum) <= number <= Decimal(maximum): raise ValueError(f'Amount must be between {minimum} and {maximum}')
    return format(number, 'f')


def local_endpoint(value):
    parsed = urlsplit(value)
    if parsed.scheme not in ('http','https') or parsed.hostname not in ('localhost','127.0.0.1','::1') or \
       parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('','/','/v1','/v1/'):
        raise ValueError('Use a local OpenAI-compatible endpoint, e.g. http://127.0.0.1:11434/v1')
    _ = parsed.port
    return value.rstrip('/')


def defaults():
    return dict(schema_version=1, family='sma_cross_long', symbol='SPY', timeframe='1m',
                capital_usd='1000', order_budget_usd='1000', profit_target_usd='0', loss_stop_usd='50',
                liquidity='balanced', ai=dict(provider='none', model='', endpoint='http://127.0.0.1:11434/v1',
                                            consent=False, daily_call_limit=20))


def validate(value):
    value = deepcopy(value)
    if set(value) != set(defaults()) or type(value['schema_version']) is not int or value['schema_version'] != 1:
        raise ValueError('Invalid setup settings')
    if value['family'] not in FAMILIES or not re.fullmatch(r'[A-Z][A-Z0-9]{0,4}(?:[.-][A-Z])?', value['symbol']):
        raise ValueError('Choose a supported US stock/ETF strategy and symbol')
    if value['timeframe'] not in ('1m','5m','15m','1h','1d') or value['liquidity'] not in LIQUIDITY:
        raise ValueError('Invalid timeframe or liquidity level')
    for name in ('capital_usd','order_budget_usd','profit_target_usd','loss_stop_usd'):
        value[name] = amount(value[name], '0' if name == 'profit_target_usd' else '.01')
    if Decimal(value['order_budget_usd']) > min(Decimal(value['capital_usd']), Decimal('1000')):
        raise ValueError('Order budget must fit your capital and the $1,000 paper entry cap')
    if Decimal(value['loss_stop_usd']) > Decimal(value['capital_usd']): raise ValueError('Loss stop must fit your capital')
    ai = value['ai']
    if type(ai) is not dict or set(ai) != set(defaults()['ai']) or ai['provider'] not in ('none','local','codex','claude'):
        raise ValueError('Invalid AI provider')
    if not isinstance(ai['model'], str) or len(ai['model']) > 128 or (ai['model'] and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]*', ai['model'])):
        raise ValueError('Invalid model identifier')
    if ai['provider'] == 'local' and not ai['model']: raise ValueError('A local model identifier is required')
    ai['endpoint'] = local_endpoint(ai['endpoint'])
    if type(ai['consent']) is not bool or type(ai['daily_call_limit']) is not int or not 1 <= ai['daily_call_limit'] <= 100:
        raise ValueError('Invalid AI consent or request budget')
    return value


def fingerprint(profile):
    return hashlib.sha256(json.dumps(validate(profile), sort_keys=True, separators=(',',':')).encode()).hexdigest()


def load_profile():
    if not PROFILE.exists(): return None
    if PROFILE.is_symlink(): raise ValueError('Settings symlink refused')
    return validate(json.loads(PROFILE.read_text()))


def save_profile(profile):
    from paper import save
    profile = validate(profile)
    save(PROFILE, profile)
    return profile


def ask(label, default, convert=lambda x:x):
    while True:
        text = input(f'{label} [{default}]: ').strip() or str(default)
        try: return convert(text)
        except (ValueError, InvalidOperation) as error: print(str(error))


def choice(label, options, current):
    print(label)
    for i, (key, title) in enumerate(options, 1): print(f'  {i}) {title}')
    default = next(i for i,(key,_) in enumerate(options, 1) if key == current)
    def convert(text):
        if text.isdigit() and 1 <= int(text) <= len(options): return options[int(text)-1][0]
        raise ValueError('Choose one of the numbered options')
    return ask('Choice', default, convert)


def wizard(interactive=True, *, mode=None):
    import os
    current = load_profile()
    if not interactive:
        if current is None: save_profile(defaults())
        print('Saved setup settings; AI is off unless explicitly enabled in sh tets setup.')
        return current or defaults()
    p = current or defaults()
    print('\nALGO SETUP · paper execution / research · live orders unavailable')
    if os.environ.get('TETS_SKIP_LEGACY_AI_PROMPT') != '1':
        previous_provider = p['ai']['provider']
        p['ai']['provider'] = choice('AI connection', [('none','Rule-based only'),('local','Local OpenAI-compatible model (Ollama / LM Studio)'),
                                        ('codex','My signed-in Codex CLI'),('claude','My signed-in Claude CLI')], p['ai']['provider'])
        provider = p['ai']['provider']
        if provider != previous_provider: p['ai']['model'] = ''
        if provider != 'none':
            p['ai']['model'] = ask('Model ID (blank uses CLI default)', p['ai']['model'])
            if provider == 'local':
                while not p['ai']['model']: p['ai']['model'] = ask('Local model ID required', '')
                p['ai']['endpoint'] = ask('Local API base URL', p['ai']['endpoint'], local_endpoint)
            print('AI receives recent public bars plus your chosen budget/goal. CLI reviews use your AI plan.')
            p['ai']['consent'] = ask('Enable bounded AI entry reviews? y/N', 'n', lambda text:text.lower() in ('y','yes'))
            p['ai']['daily_call_limit'] = ask('Maximum AI calls per day (1–100)', p['ai']['daily_call_limit'],
                lambda text:int(text) if text.isdigit() and 1 <= int(text) <= 100 else (_ for _ in ()).throw(ValueError('Enter 1–100')))
            if provider in ('codex','claude') and not shutil.which(provider):
                print(f'{provider} CLI is unavailable. Install/sign in yourself; the panel will report unavailable until it works.')
        else: p['ai']['consent'] = False
    family_options = list(zip(FAMILIES, ['Moving-average trend','Momentum','Mean reversion','Breakout','Upstream EMA demo']))
    timeframe_options = [(t,t) for t in ('1m','5m','15m','1h','1d')]
    if mode == 'shadow':
        family_options = [opt for opt in family_options if opt[0] != 'upstream_ema']
        timeframe_options = [opt for opt in timeframe_options if opt[0] != '1d']
        if p['family'] == 'upstream_ema': p['family'] = 'sma_cross_long'
        if p['timeframe'] == '1d': p['timeframe'] = '1m'
    p['family'] = choice('Algorithm type', family_options, p['family'])
    p['symbol'] = ask('US stock / ETF symbol', p['symbol'], lambda text:text.upper() if re.fullmatch(r'[A-Za-z][A-Za-z0-9]{0,4}(?:[.-][A-Za-z])?', text) else (_ for _ in ()).throw(ValueError('Enter a US stock / ETF symbol')))
    p['timeframe'] = choice('Decision bars', timeframe_options, p['timeframe'])
    p['capital_usd'] = ask('Capital allocated to this algo in USD (no deposit)', p['capital_usd'], lambda text:amount(text,'.01'))
    cap = min(Decimal(p['capital_usd']), Decimal('1000'))
    p['order_budget_usd'] = ask('Maximum entry budget USD', str(min(cap, Decimal(p['order_budget_usd']))), lambda text:amount(text,'.01',str(cap)))
    p['profit_target_usd'] = ask('Desired profit USD for this strategy run (0 = no target)', p['profit_target_usd'], amount)
    print('Profit target is a goal, not a forecast. It never increases position size or relaxes limits.')
    p['loss_stop_usd'] = ask('Loss amount that stops new entries USD', str(min(Decimal(p['capital_usd']), Decimal(p['loss_stop_usd']))),
                             lambda text:amount(text,'.01',p['capital_usd']))
    p['liquidity'] = choice('Market liquidity requirement (IEX spread / recent traded dollars)',
        [('high','High — spread ≤5 bps / average bar dollars ≥$100,000'),('balanced','Balanced — ≤15 bps / ≥$25,000'),('flexible','Flexible — ≤30 bps / ≥$5,000')], p['liquidity'])
    save_profile(p)
    print('Saved. The Tets status page will show connection status and actual activity.')
    print('If the broker is already connected, restart the workstation to apply changes. Entries pause until then.')
    print('Paper mode uses these settings. Yahoo shadow uses intraday strategy rules, capital and entry budget; AI/liquidity reviews apply only to paper mode.')
    return p


def public_settings(profile):
    return {**profile, 'profile_sha256': fingerprint(profile), 'liquidity_limits': LIQUIDITY[profile['liquidity']],
            'target_kind':'user_goal_not_forecast', 'ai':{k:v for k,v in profile['ai'].items() if k != 'endpoint'}}


if __name__ == '__main__':
    try: wizard(sys.stdin.isatty())
    except (KeyboardInterrupt, EOFError): sys.exit(1)
    except ValueError as error: print(f'Setup: {error}', file=sys.stderr); sys.exit(1)
