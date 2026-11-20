#!/usr/bin/env python3
"""Resumable CWL collector for historical and live seasons.

Run discover nightly with the same season, output folder and group cap.
Place a verified seasonal catalog at ROOT/seeds_used.json or pass --seeds.
Until then, --previous supplies candidates and --search-pages enables API sampling.
"""
import argparse
import hashlib
import json
import re
import sys
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

DIVISIONS = [(f'{tier.lower()}-{number}', f'{tier} League {roman}')
             for tier in ('Bronze', 'Silver', 'Gold', 'Crystal', 'Master', 'Champion', 'Titan')
             for number, roman in ((3, 'III'), (2, 'II'), (1, 'I'))] + [('legend', 'Legend League')]
TAG_RE = r'[0289PYLQGRJCUV]+'


def now():
    return datetime.now(timezone.utc).isoformat()


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    tmp.replace(path)


def validate_catalog(catalog):
    if catalog.get('schema_version') != 1 or not re.fullmatch(r'\d{4}-\d{2}', catalog.get('season', '')):
        raise ValueError('Invalid seed catalog schema/season')
    seen = {}
    names = dict(DIVISIONS)
    for slug, division in catalog.get('divisions', {}).items():
        if slug not in names or division.get('name') != names[slug]:
            raise ValueError(f'Unknown division: {slug}')
        expected = f"https://clashspot.net/en/rankings/clan-war-leagues/{catalog['season']}-01/{slug}"
        if division.get('source_url') != expected or not (division.get('html_sha256') or division.get('evidence_sha256')):
            raise ValueError(f'Missing historical provenance: {slug}')
        if not 1 <= len(division.get('seeds', [])) <= 200:
            raise ValueError(f'Invalid seed count: {slug}')
        local = set()
        for seed in division['seeds']:
            tag = seed['tag']
            if not re.fullmatch('#' + TAG_RE, tag) or tag in local:
                raise ValueError(f'Invalid/duplicate clan tag in {slug}: {tag}')
            local.add(tag)
            if tag in seen and seen[tag] != slug:
                raise ValueError(f'Conflicting historical league for {tag}: {seen[tag]} / {slug}')
            seen[tag] = slug
    return seen



import os
import random
import string
import shutil
import fcntl
from urllib.parse import quote
from itertools import chain
from itertools import islice

BASE = 'https://api.clashofclans.com/v1'


class BudgetExceeded(Exception): pass
class NotFound(Exception): pass


class Client:
    def __init__(self, budget, delay):
        token = os.environ.get('COC_API_TOKEN', '').strip()
        if not token: raise ValueError('Set COC_API_TOKEN in your environment')
        try:
            import requests
        except ImportError as exc:
            raise ValueError('Install requests with python -m pip install requests') from exc
        self.request_error = requests.RequestException
        self.session = requests.Session()
        self.session.headers.update(Authorization='Bearer ' + token, Accept='application/json')
        self.budget, self.delay, self.count = budget, delay, 0

    def get(self, path):
        for attempt in range(3):
            if self.count >= self.budget: raise BudgetExceeded()
            if self.count: time.sleep(self.delay)
            self.count += 1
            try:
                response = self.session.get(BASE + path, timeout=25)
            except self.request_error as exc:
                if attempt == 2: raise RuntimeError(f'API connection failed: {type(exc).__name__}') from exc
                time.sleep(2 ** attempt)
                continue
            if response.status_code == 404: raise NotFound(path)
            if response.status_code in (401, 403):
                raise RuntimeError(f'API HTTP {response.status_code}; stopped, check API token/access')
            if response.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            if not response.ok: raise RuntimeError(f'API HTTP {response.status_code} on {path}')
            return response.json()
        raise RuntimeError('API retries exhausted')


def group_id(season, tags):
    return hashlib.sha256((season + ':' + ','.join(sorted(tags))).encode()).hexdigest()[:20]


def group_folder(root, record):
    if record.get('league_id') and record.get('group_folder'):
        return root / record['league_id'] / record['group_folder']
    return root / (record['division_slug'] or '_unclassified') / record['group_id']


def war_file(root, record, tag):
    return group_folder(root, record) / 'guerras' / (tag.lstrip('#') + '.json')


def war_mapping(group):
    result = {}
    for rnd, entry in enumerate(group.get('rounds', []), 1):
        for tag in entry.get('warTags', []):
            if tag and tag != '#0':
                if tag in result: raise ValueError(f'Duplicate war tag: {tag}')
                result[tag] = rnd
    if len(result) > 28: raise ValueError('Group has more than 28 war tags')
    return result


def save_state(root, state):
    state['season_month'] = state['season']
    state['team_sizes'] = [15, 30]
    for rec in list(state['groups'].values()) + list(state.get('archived', {}).values()):
        rec.update(season_month=state['season'], war_tags=sorted(rec['wars']),
                   league_name_verified=rec['division_name'], verification_status='verified' if rec['division_slug'] else 'pending')
        rec.setdefault('league_name_observed', None)
        rec.setdefault('league_id_observed', None)
    save(root / 'manifest.json', state)


def snapshot(root, record, group):
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    save(group_folder(root, record) / 'snapshots' / (stamp + '.json'), group)


def ensure_league_catalog(root, client):
    path = root / 'warleagues.json'
    catalog = read(path) if path.exists() else client.get('/warleagues?limit=100')
    mapping = {}
    for item in catalog.get('items', []):
        identifier = str(item.get('id', ''))
        if item.get('name') in dict(DIVISIONS).values():
            if not identifier.isdigit(): raise ValueError('Invalid league ID returned by API')
            if item['name'] in mapping: raise ValueError('Duplicate league name returned by API')
            mapping[item['name']] = identifier
    if set(mapping) != set(dict(DIVISIONS).values()):
        raise ValueError('Official /warleagues catalog does not contain all 22 historical division names')
    save(path, catalog)
    return mapping


def assign_location(root, state, record, leagues):
    identifier = leagues[record['division_name']]
    used = {r.get('group_folder') for r in list(state['groups'].values()) + list(state.get('archived', {}).values())
            if r.get('league_id') == identifier}
    ordinal = 1
    while (f'grupo_{ordinal:03d}' in used or (root / identifier / f'grupo_{ordinal:03d}').exists()
           or (root / '_pruned' / identifier / f'grupo_{ordinal:03d}').exists()):
        ordinal += 1
    record.update(league_id=identifier, group_folder=f'grupo_{ordinal:03d}')


def recover_layout(root, state):
    journal = root / 'layout_pending.json'
    if not journal.exists(): return
    item = read(journal)
    src, dst = root / item['old'], root / item['new']
    if not src.resolve().is_relative_to(root) or not dst.resolve().is_relative_to(root):
        raise ValueError('Unsafe layout migration path')
    if src.exists() and dst.exists(): raise ValueError('Ambiguous layout migration: both paths exist')
    if src.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)
    if not dst.exists(): raise ValueError('Missing folder during layout migration')
    rec = item['record']
    state[item['bucket']][rec['group_id']] = rec
    save_state(root, state)
    base = root / '_pruned' if item['bucket'] == 'archived' else root
    group = read(dst / 'league_group.json')
    export_rosters(base, rec, group)
    if not (dst / 'snapshots').exists(): snapshot(base, rec, group)
    save(dst / 'historical_source.json', rec)
    rebuild_index(root, state)
    journal.unlink()


def prepare_layout(root, state, leagues):
    legacy = [(bucket, r) for bucket in ('groups', 'archived') for r in state.get(bucket, {}).values()
              if r['division_slug'] and (not r.get('league_id') or not r.get('group_folder'))]
    if legacy:
        backup = root / 'layout_backups' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        backup.mkdir(parents=True)
        for name in ('manifest.json', 'guerras_index.json'):
            if (root / name).exists(): shutil.copy2(root / name, backup / name)
    for bucket, old in legacy:
        base = root / '_pruned' if bucket == 'archived' else root
        rec = dict(old)
        assign_location(root, state, rec, leagues)
        src, dst = group_folder(base, old), group_folder(base, rec)
        save(root / 'layout_pending.json', {'old': str(src.relative_to(root)),
             'new': str(dst.relative_to(root)), 'bucket': bucket, 'record': rec})
        recover_layout(root, state)
        print(f"Layout: {src.relative_to(root)} -> {dst.relative_to(root)}")
    state['storage_layout'] = 'retry-league-id-grupo-v1'
    save_state(root, state)


def validate_war(war, record):
    sides = {war.get(side, {}).get('tag') for side in ('clan', 'opponent')}
    if len(sides) != 2 or not sides.issubset(set(record['clan_tags'])):
        raise ValueError(f"War clans do not match group {record['group_id']}")
    size = war.get('teamSize')
    if size not in (15, 30) or record.get('team_size') not in (None, size):
        raise ValueError('Inconsistent war team size')
    return size


def finished(root, record, tag):
    path = war_file(root, record, tag)
    if not path.exists(): return False
    war = read(path)
    validate_war(war, record)
    return war.get('state') == 'warEnded'


def download_wars(root, state, record, client):
    if len(record['wars']) < 28:
        try:
            group = client.get('/clans/' + quote(record['seed_clan_tag'], safe='') + '/currentwar/leaguegroup')
        except NotFound:
            group = None
        if group is not None:
            tags = [c.get('tag') for c in group.get('clans', [])]
            if (group.get('season', '')[:7] != state['season']
                    or sorted(tags) != record['clan_tags']):
                raise ValueError('Refreshed snapshot belongs to another group/season')
            mapping = war_mapping(group)
            if not set(record['wars']).issubset(mapping):
                raise ValueError('Refreshed group lost previously known war tags')
            other_wars = {t for r in state['groups'].values()
                          if r['group_id'] != record['group_id'] for t in r['wars']}
            if other_wars.intersection(mapping): raise ValueError('Refreshed group shares war tags')
            record['wars'] = mapping
            snapshot(root, record, group)
            save(group_folder(root, record) / 'league_group.json', group)
            export_rosters(root, record, group)
            save_state(root, state)
    for tag, rnd in record['wars'].items():
        if finished(root, record, tag): continue
        try: war = client.get('/clanwarleagues/wars/' + quote(tag, safe=''))
        except NotFound:
            record.setdefault('war_errors', {})[tag] = {'http': 404, 'at_utc': now()}
            save_state(root, state)
            continue
        size = validate_war(war, record)
        record['team_size'] = size
        save(war_file(root, record, tag), war)
        record.setdefault('war_errors', {}).pop(tag, None)
        save_state(root, state)
    # Rebuilt from the manifest by extract/collect; raw responses remain untouched.


def export_rosters(root, record, group):
    for clan in group['clans']:
        if not isinstance(clan.get('members'), list): raise ValueError('Missing CWL registered roster')
        folder = group_folder(root, record) / 'clanes' / clan['tag'].lstrip('#')
        save(folder / 'inscripcion.json', {**{k: v for k, v in clan.items() if k != 'members'},
             'season': record['season'], 'group_id': record['group_id'],
             'division_observed': record.get('league_name_observed'),
             'division_verified': record['division_name'], 'verification_status': ('verified' if record['division_slug'] else 'pending'),
             'source': 'league_group.json',
             'division_historical': record['division_name'], 'source_url': record['source_url']})
        save(folder / 'miembros_inscritos.json', clan['members'])


def rebuild_index(root, state):
    wars = {}
    for record in state['groups'].values():
        for tag, rnd in record['wars'].items():
            if tag in wars: raise ValueError('War present in two groups: ' + tag)
            file = war_file(root, record, tag)
            wars[tag] = {'group_id': record['group_id'], 'division': record['division_name'],
                'league_id': record.get('league_id'), 'group_folder': record.get('group_folder'),
                'verification_status': ('verified' if record['division_slug'] else 'pending'), 'team_size': record.get('team_size'),
                'division_slug': record['division_slug'], 'season': state['season'], 'round': rnd,
                'file': str(file), 'saved': file.exists(), 'finished': finished(root, record, tag)}
    save(root / 'guerras_index.json', {'season': state['season'], 'season_month': state['season'], 'wars': wars})


def counts(state, slug):
    return sum(r['division_slug'] == slug for r in state['groups'].values())


def summary(root, state, cap, catalog, verbose=False):
    total, complete = 0, 0
    for record in state['groups'].values():
        n = sum(finished(root, record, tag) for tag in record['wars'])
        total += n
        complete += len(record['wars']) == 28 and n == 28
    expected = sum(len(r['wars']) for r in state['groups'].values())
    print(f"Season {state['season']}: {len(state['groups'])} groups; {complete} with 28 finished wars; "
          f'{total}/{expected} known wars finished')
    print(f"Unclassified groups: {sum(not r['division_slug'] for r in state['groups'].values())}")
    if verbose:
        for slug, name in DIVISIONS:
            seeds = catalog['divisions'].get(slug, {}).get('seeds', [])
            done = sum(t['tag'] in state['attempts'] for t in seeds)
            print(f'  {name}: {counts(state, slug)}/{cap} groups; {done}/{len(seeds)} seeds examined')


def ranking_fields(catalog, slug, seed):
    division = catalog['divisions'][slug]
    return dict(division_slug=slug, division_name=division['name'],
                division_basis='historical_clashspot_ranking',
                source_url=division['source_url'],
                source_detail_url=seed.get('detail_url'),
                source_evidence_sha256=division.get('evidence_sha256') or division.get('html_sha256'),
                source_captured_at_utc=division.get('captured_at_utc'))


def live_league(client, group, tag, season, leagues):
    active_states = {'preparation', 'inWar'}
    if season != datetime.now(timezone.utc).strftime('%Y-%m') or group.get('state') not in active_states:
        return {}
    path = '/clans/' + quote(tag, safe='')
    try:
        profile = client.get(path)
        observed_at = now()
        league = profile.get('warLeague') or {}
        name = league.get('name')
        if profile.get('tag') != tag or name not in leagues or str(league.get('id')) != leagues[name]:
            return {}
        confirmed = client.get(path + '/currentwar/leaguegroup')
    except NotFound:
        return {}
    if (confirmed.get('state') not in active_states or confirmed.get('season', '')[:7] != season
            or {clan.get('tag') for clan in confirmed.get('clans', [])}
            != {clan.get('tag') for clan in group.get('clans', [])}):
        return {}
    return dict(division_slug=next(slug for slug, label in DIVISIONS if label == name),
                division_name=name, division_basis='live_clan_war_league',
                league_name_observed=name, league_id_observed=leagues[name],
                source_clan_tag=tag, source_captured_at_utc=observed_at,
                source_url=BASE + path)


def classify_pending(root, state, catalog, ranked, client):
    for gid, old in list(state['groups'].items()):
        matches = [ranked[tag] for tag in old['clan_tags'] if tag in ranked]
        divisions = {slug for slug, _ in matches}
        if len(divisions) > 1 or (divisions and old['division_slug'] and old['division_slug'] not in divisions):
            raise ValueError(f'Conflicting seasonal ranking for group {gid}')
        if old['division_slug']:
            continue
        if matches:
            slug, seed = matches[0]
            fields = ranking_fields(catalog, slug, seed)
        elif state['season'] == datetime.now(timezone.utc).strftime('%Y-%m'):
            tag = old['seed_clan_tag']
            try:
                group = client.get('/clans/' + quote(tag, safe='') + '/currentwar/leaguegroup')
            except NotFound:
                continue
            if (group.get('season', '')[:7] != state['season']
                    or {clan.get('tag') for clan in group.get('clans', [])} != set(old['clan_tags'])):
                continue
            fields = live_league(client, group, tag, state['season'], state['league_ids_by_name'])
        else:
            continue
        if not fields:
            continue
        record = {**old, **fields}
        assign_location(root, state, record, state['league_ids_by_name'])
        save(root / 'layout_pending.json', {
            'old': str(group_folder(root, old).relative_to(root)),
            'new': str(group_folder(root, record).relative_to(root)),
            'bucket': 'groups', 'record': record,
        })
        recover_layout(root, state)


def previous_tags(path):
    if not path:
        return
    catalog_path = path / 'seeds_used.json'
    if catalog_path.exists():
        catalog = read(catalog_path)
        yield from validate_catalog(catalog)
    manifest = path / 'manifest.json'
    if manifest.exists():
        for record in read(manifest)['groups'].values():
            yield from record['clan_tags']


def search_tags(args, client):
    for _ in range(args.search_pages):
        name = ''.join(random.choices(string.ascii_lowercase, k=3))
        params = [f'name={name}', 'limit=100']
        for key, value in (('minMembers', args.min_members),
                           ('maxMembers', args.max_members),
                           ('minClanLevel', args.min_clan_level)):
            if value is not None:
                params.append(f'{key}={value}')
        for clan in client.get('/clans?' + '&'.join(params)).get('items', []):
            if args.max_clan_level is None or clan['clanLevel'] <= args.max_clan_level:
                yield clan['tag']


def capture_candidate(args, root, state, catalog, ranked, client, known, tag):
    if tag in known:
        return
    attempt = state['attempts'].get(tag, {})
    if attempt.get('status') == 'invalid':
        return
    if attempt.get('status') == 'unavailable' and not args.retry_unavailable:
        if (datetime.now(timezone.utc) - datetime.fromisoformat(attempt['at_utc'])).total_seconds() < 3600:
            return
    try:
        group = client.get('/clans/' + quote(tag, safe='') + '/currentwar/leaguegroup')
    except NotFound:
        state['attempts'][tag] = dict(status='unavailable', at_utc=now())
        save_state(root, state)
        return
    tags = [clan.get('tag') for clan in group.get('clans', [])]
    if group.get('season', '')[:7] != state['season'] or len(tags) != 8 or not all(tags) or len(set(tags)) != 8 or tag not in tags:
        state['attempts'][tag] = dict(status='pending', at_utc=now())
        save_state(root, state)
        return
    matches = [ranked[t] for t in tags if t in ranked]
    if len({slug for slug, _ in matches}) > 1:
        raise ValueError(f'Conflicting seasonal ranking for group of {tag}')
    if matches:
        slug, seed = matches[0]
        classification = ranking_fields(catalog, slug, seed)
    else:
        classification = live_league(client, group, tag, state['season'], state['league_ids_by_name'])
    slug = classification.get('division_slug')
    if slug and ((args.division and slug not in args.division) or counts(state, slug) >= args.max_per_division):
        state['attempts'][tag] = dict(status='outside_target', at_utc=now())
        return
    if not classification:
        classification = dict(division_slug=None, division_name=None, source_url=None)
    wars = war_mapping(group)
    if set(wars).intersection(t for r in state['groups'].values() for t in r['wars']):
        raise ValueError('Overlapping war tags in distinct groups')
    gid = group_id(state['season'], tags)
    record = dict(group_id=gid, season=state['season'], seed_clan_tag=tag,
                  captured_at_utc=now(), clan_tags=sorted(tags), wars=wars,
                  team_size=None, **classification)
    if slug:
        assign_location(root, state, record, state['league_ids_by_name'])
    export_rosters(root, record, group)
    snapshot(root, record, group)
    save(group_folder(root, record) / 'league_group.json', group)
    if slug:
        save(group_folder(root, record) / 'historical_source.json', record)
    state['groups'][gid] = record
    state['attempts'][tag] = dict(status='accepted', group_id=gid, at_utc=now())
    known.update(tags)
    save_state(root, state)
    download_wars(root, state, record, client)


def collect(args, root, state, catalog, client):
    ranked = {seed['tag']: (slug, seed) for slug, division in catalog['divisions'].items()
              for seed in division['seeds']}
    classify_pending(root, state, catalog, ranked, client)
    known = {tag for bucket in ('groups', 'archived') for rec in state[bucket].values()
             for tag in rec['clan_tags']}
    for record in state['groups'].values():
        if not args.division or record['division_slug'] in args.division or not record['division_slug']:
            download_wars(root, state, record, client)
    if ranked:
        candidates = (tag for tag, (slug, _) in ranked.items()
                      if (not args.division or slug in args.division)
                      and counts(state, slug) < args.max_per_division)
    else:
        previous = sorted(
            (tag for tag in unique_tags(previous_tags(args.previous)) if tag not in known),
            key=lambda tag: state['attempts'].get(tag, {}).get('at_utc', ''),
        )
        candidates = chain(islice(previous, args.previous_limit), search_tags(args, client))
    for tag in unique_tags(candidates):
        capture_candidate(args, root, state, catalog, ranked, client, known, tag)


def unique_tags(candidates):
    seen = set()
    for tag in candidates:
        if tag not in seen:
            seen.add(tag)
            yield tag


def profiles(root, state, client):
    saved = 0
    for rec in state['groups'].values():
        for tag in rec['clan_tags']:
            folder = group_folder(root, rec) / 'clanes' / tag.lstrip('#')
            path = folder / 'perfil_clan.json'
            if path.exists(): continue
            try: clan = client.get('/clans/' + quote(tag, safe=''))
            except NotFound: continue
            if clan.get('tag') != tag: raise ValueError('Profile tag mismatch')
            save(path, clan)
            save(folder / 'perfil_captura.json', {'captured_at_utc': now(), 'note': 'Current profile; never used for historical classification'})
            saved += 1
    print(f'Current profiles saved: {saved}')


def recover_moves(root, state):
    """Finish a journaled archive/restore after interruption."""
    pending = state.get('pending_moves', [])
    for item in pending:
        src, dst = root / item['from'], root / item['to']
        if src.exists() and dst.exists():
            raise ValueError('Both relocation paths exist; inspect before continuing')
        if src.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
        if not dst.exists(): raise ValueError('Missing group folder during relocation')
        gid = item['group_id']
        source_key, target_key = ('groups', 'archived') if item['archive'] else ('archived', 'groups')
        record = state[source_key].pop(gid)
        state.setdefault(target_key, {})[gid] = record
    if pending:
        state.pop('pending_moves', None)
        save_state(root, state)
        rebuild_index(root, state)


def relocate(root, state, records, archive):
    pending = []
    for rec in records:
        normal = group_folder(root, rec).relative_to(root)
        archived = Path('_pruned') / normal
        src, dst = (normal, archived) if archive else (archived, normal)
        pending.append({'group_id': rec['group_id'], 'from': str(src), 'to': str(dst), 'archive': archive})
    state['pending_moves'] = pending
    save_state(root, state)
    recover_moves(root, state)


def prune(root, state, cap, dry_run):
    selected = []
    for slug, name in DIVISIONS:
        records = [r for r in state['groups'].values() if r['division_slug'] == slug]
        # Prefer groups with more completed wars, then more known wars, stable ID as tie breaker.
        records.sort(key=lambda r: (-sum(finished(root, r, t) for t in r['wars']), -len(r['wars']), r['group_id']))
        for rec in records[cap:]:
            selected.append(rec)
            print(f"{'WOULD ARCHIVE' if dry_run else 'ARCHIVE'} {name}: {rec['group_id']}")
    if not dry_run and selected: relocate(root, state, selected, True)
    print(f'{len(selected)} groups selected. Pruned data is retained in _pruned; restore reverses this.')


def run(args):
    root = args.root.resolve()
    seed_path = args.seeds or root / 'seeds_used.json'
    catalog = read(seed_path) if args.seeds or seed_path.exists() else {
        'schema_version': 1, 'season': args.season, 'divisions': {}}
    sources = validate_catalog(catalog)
    if catalog['season'] != args.season:
        raise ValueError('Seed catalog belongs to another season. Use --previous for old candidates.')
    path = root / 'manifest.json'
    state = read(path) if path.exists() else {'schema_version': 'clashspot-cwl-v1', 'season': catalog['season'], 'groups': {}, 'attempts': {}, 'archived': {}}
    if state.get('schema_version') != 'clashspot-cwl-v1' or state.get('season') != catalog['season']:
        raise ValueError('Output belongs to another pipeline/season; use a new folder')
    recover_moves(root, state)
    recover_layout(root, state)
    for rec in list(state['groups'].values()) + list(state.get('archived', {}).values()):
        if rec['division_slug'] and {sources[t] for t in rec['clan_tags'] if t in sources} - {rec['division_slug']}:
            raise ValueError('Catalog conflicts with a saved historical group')
    if args.command == 'status':
        summary(root, state, args.max_per_division, catalog, True)
        print(f"Archived groups: {len(state.get('archived', {}))}")
        return
    if args.command == 'prune':
        prune(root, state, args.max_per_division, args.dry_run)
        return
    if args.command == 'restore':
        records = [state.get('archived', {}).get(gid) for gid in args.group_id]
        if any(r is None for r in records): raise ValueError('Unknown archived group ID')
        relocate(root, state, records, False)
        return
    if args.command == 'rosters':
        for rec in state['groups'].values():
            export_rosters(root, rec, read(group_folder(root, rec) / 'league_group.json'))
        print('Registered rosters rebuilt from group snapshots')
        return
    client = Client(args.max_requests, args.delay)
    if args.seeds:
        save(root / 'seeds_used.json', catalog)
    try:
        state['league_ids_by_name'] = ensure_league_catalog(root, client)
        prepare_layout(root, state, state['league_ids_by_name'])
        if args.command in ('collect', 'discover', 'load'):
            collect(args, root, state, catalog, client)
        elif args.command == 'extract':
            for rec in state['groups'].values():
                if not args.division or rec['division_slug'] in args.division:
                    download_wars(root, state, rec, client)
        elif args.command == 'profiles': profiles(root, state, client)
    except BudgetExceeded:
        print('Request budget exhausted; repeat the same command to resume')
    finally:
        save_state(root, state)
        rebuild_index(root, state)
        summary(root, state, args.max_per_division, catalog, args.verbose)
        print(f'API requests this run: {client.count}')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    def common(parser, suppressed=False):
        defaults = argparse.SUPPRESS if suppressed else None
        parser.add_argument('--root', type=Path, default=defaults)
    common(p)
    sub = p.add_subparsers(dest='command', required=True)
    c = sub.add_parser('discover', aliases=['collect', 'load'], help='Resume capture from historical seeds, lowest to highest league')
    c.add_argument('--previous', type=Path, help='Previous season dataset directory')
    c.add_argument('--previous-limit', type=int, default=200)
    c.add_argument('--search-pages', type=int, default=0)
    c.add_argument('--min-members', type=int)
    c.add_argument('--max-members', type=int)
    c.add_argument('--min-clan-level', type=int)
    c.add_argument('--max-clan-level', type=int)
    c.add_argument('--retry-unavailable', action='store_true')
    c.add_argument('--division', action='append', choices=[x[0] for x in DIVISIONS])
    for name in ('extract', 'profiles', 'status', 'rosters', 'prune', 'restore'):
        cmd = sub.add_parser(name)
        if name == 'extract': cmd.add_argument('--division', action='append', choices=[x[0] for x in DIVISIONS])
        if name == 'prune': cmd.add_argument('--dry-run', action='store_true')
        if name == 'restore': cmd.add_argument('--group-id', action='append', required=True)
    # aliases refer to the same parser: configure each only once.
    for parser in {id(v): v for v in sub.choices.values()}.values():
        common(parser, True)
        parser.add_argument('--season', required=True)
        parser.add_argument('--seeds', type=Path)
        parser.add_argument('--max-per-division', type=int, default=50)
        parser.add_argument('--verbose', action='store_true')
        parser.add_argument('--max-requests', type=int, default=3000)
        parser.add_argument('--delay', type=float, default=0.2)
    args = p.parse_args()
    if args.max_per_division < 1 or args.max_requests < 1 or args.delay < 0: p.error('Invalid cap/budget/delay')
    if not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', args.season):
        p.error('--season must be YYYY-MM')
    if getattr(args, 'search_pages', 0) < 0 or getattr(args, 'previous_limit', 0) < 0:
        p.error('Search pages and previous limit must be nonnegative')
    for low, high in (('min_members', 'max_members'), ('min_clan_level', 'max_clan_level')):
        minimum, maximum = getattr(args, low, None), getattr(args, high, None)
        if any(value is not None and value < 1 for value in (minimum, maximum)):
            p.error('Search filters must be positive')
        if minimum is not None and maximum is not None and minimum > maximum:
            p.error(f'--{low.replace("_", "-")} exceeds --{high.replace("_", "-")}')
    if args.root is None:
        args.root = Path(__file__).resolve().parent / args.season
    args.root.mkdir(parents=True, exist_ok=True)
    with (args.root / '.capture.lock').open('a') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise ValueError('Another command is using this dataset')
        run(args)


if __name__ == '__main__':
    try: main()
    except (ValueError, RuntimeError, OSError, KeyError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        sys.exit(1)
