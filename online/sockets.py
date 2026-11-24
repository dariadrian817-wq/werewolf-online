"""Real-time online multiplayer — SocketIO handlers."""
import random
import time
from flask import request
from flask_socketio import SocketIO, emit
from game_engine.engine import GameEngine

socketio = SocketIO(cors_allowed_origins="*")

rooms = {}


# ─────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────

def generate_code():
    chars = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
    return ''.join(random.choices(chars, k=6))


def normalize_code(raw_code):
    return (raw_code or '').strip().upper()


def find_room_by_sid(sid):
    for code, room in rooms.items():
        for p in room['players'].values():
            if p.get('sid') == sid:
                return code
    return None


def role_holders(room, role):
    """Return list of alive player_ids with the given role."""
    if not room.get('engine') or not role:
        return []
    return [
        pid for pid in room['player_order']
        if pid in room['players']
        and room['players'][pid].get('alive', True)
        and room.get('roles', {}).get(pid) == role
    ]


def build_public_state(room, viewer_pid=None):
    """State safe for everyone (plus viewer's own role when assigned)."""
    players_out = []
    for pid in room['player_order']:
        p = room['players'].get(pid, {})
        players_out.append({
            'id': pid,
            'name': p.get('name', '?'),
            'ready': p.get('ready', False),
            'alive': p.get('alive', True),
            'connected': p.get('sid') is not None,
        })

    state = {
        'code': room['code'],
        'host_id': room['host_id'],
        'phase': room['phase'],
        'players': players_out,
        'round': room.get('round', 0),
    }

    if viewer_pid and viewer_pid in room.get('roles', {}):
        state['your_role'] = room['roles'][viewer_pid]
        state['you_alive'] = room['players'].get(viewer_pid, {}).get('alive', True)

    return state


def broadcast_state(room):
    """Send personalized state to each connected player."""
    for pid, p in room['players'].items():
        if p.get('sid'):
            emit(
                'state',
                build_public_state(room, viewer_pid=pid),
                to=p['sid']
            )


def broadcast_to_all(room, event, data):
    for p in room['players'].values():
        if p.get('sid'):
            emit(event, data, to=p['sid'])


def advance_night(room):
    """Move to next role in night queue, or resolve."""
    ns = room['night_state']
    ns['step'] += 1
    start_night_step(room)


def start_night_step(room):
    """Begin the current night step (announce role, expect actions)."""
    ns = room['night_state']

    if ns['step'] >= len(ns['queue']):
        resolve_night(room)
        return

    role = ns['queue'][ns['step']]
    holders = role_holders(room, role)

    if not holders:
        advance_night(room)
        return

    ns['current_role'] = role
    ns['pending'] = set(holders)

    broadcast_state(room)

    for pid in holders:
        p = room['players'][pid]
        if p.get('sid'):
            emit(
                'your_turn',
                {
                    'role': role,
                    'targets': [
                        {'id': tpid, 'name': room['players'][tpid]['name']}
                        for tpid in room['player_order']
                        if tpid in room['players']
                        and room['players'][tpid].get('alive', True)
                        and not (role == 'werewolf' and room.get('roles', {}).get(tpid) == 'werewolf')
                        and not (role == 'sheriff' and tpid == pid)
                        and not (role == 'witch' and tpid == pid)
                        and not (role == 'seer' and tpid == pid)
                    ],
                },
                to=p['sid']
            )


def resolve_night(room):
    engine = room['engine']
    actions = room['night_state'].get('actions', {})

    killed_indices = engine.resolve_night_actions(actions)
    killed_names = [engine.players[i] for i in killed_indices]
    killed_pids = [
        room['player_order'][i]
        for i in killed_indices
        if 0 <= i < len(room['player_order'])
    ]

    for pid in killed_pids:
        if pid in room['players']:
            room['players'][pid]['alive'] = False
            room['players'][pid]['killed_last_night'] = True

    engine.log.append({
        'round': room['round'],
        'type': 'night',
        'killed': killed_names,
    })

    room['night_state'] = {
        'queue': [],
        'step': 0,
        'actions': {},
        'current_role': None,
        'pending': set(),
    }

    broadcast_to_all(room, 'night_resolved', {
        'killed': [
            {
                'name': room['players'][pid]['name'],
                'role': room['roles'].get(pid),
            }
            for pid in killed_pids
            if pid in room['players']
        ],
        'round': room['round'],
    })

    winner = engine.check_win()
    if winner:
        end_game(room, winner)
        return

    room['phase'] = 'day'
    broadcast_state(room)


def start_night(room):
    """Begin a new night phase."""
    engine = room['engine']
    room['round'] += 1
    room['phase'] = 'night'

    queue = []
    if engine.alive_werewolves():
        queue.append('werewolf')
    if engine.find_role_index('sheriff') is not None:
        queue.append('sheriff')
    if engine.find_role_index('witch') is not None:
        queue.append('witch')
    if engine.find_role_index('doctor') is not None:
        queue.append('doctor')
    if engine.find_role_index('seer') is not None:
        queue.append('seer')

    room['night_state'] = {
        'queue': queue,
        'step': 0,
        'actions': {},
        'current_role': None,
        'pending': set(),
    }
    start_night_step(room)


def start_vote(room):
    """Begin voting phase."""
    room['phase'] = 'vote'
    room['vote_state'] = {
        'votes': {},
        'pending': set(
            pid for pid in room['player_order']
            if pid in room['players'] and room['players'][pid].get('alive', True)
        ),
    }
    broadcast_state(room)
    broadcast_to_all(room, 'vote_started', {
        'targets': [
            {'id': pid, 'name': room['players'][pid]['name']}
            for pid in room['player_order']
            if pid in room['players'] and room['players'][pid].get('alive', True)
        ]
    })


def resolve_vote(room):
    vs = room['vote_state']
    votes = vs['votes']

    tally = {}
    for _, target in votes.items():
        if target:
            tally[target] = tally.get(target, 0) + 1

    eliminated_pid = None
    if tally:
        max_votes = max(tally.values())
        candidates = [t for t, c in tally.items() if c == max_votes]
        if len(candidates) == 1:
            eliminated_pid = candidates[0]

    eliminated_name = None
    eliminated_role = None
    if eliminated_pid and eliminated_pid in room['players']:
        eliminated_name = room['players'][eliminated_pid]['name']
        eliminated_role = room['roles'].get(eliminated_pid)
        room['players'][eliminated_pid]['alive'] = False
        idx = room['player_order'].index(eliminated_pid)
        room['engine'].alive[idx] = False
        room['engine'].log.append({
            'round': room['round'],
            'type': 'vote',
            'eliminated': eliminated_name,
        })

    broadcast_to_all(room, 'vote_resolved', {
        'eliminated': {'name': eliminated_name, 'role': eliminated_role} if eliminated_pid else None,
        'tally': {
            room['players'][pid]['name']: count
            for pid, count in tally.items()
            if pid in room['players']
        },
    })

    winner = room['engine'].check_win()
    if winner:
        end_game(room, winner)
        return

    start_night(room)


def end_game(room, winner):
    room['phase'] = 'ended'
    room['winner'] = winner
    all_roles = [
        {
            'name': room['players'][pid]['name'],
            'role': room['roles'].get(pid),
            'alive': room['players'][pid].get('alive', False),
        }
        for pid in room['player_order']
        if pid in room['players']
    ]
    broadcast_to_all(room, 'game_over', {
        'winner': winner,
        'roles': all_roles,
        'log': room['engine'].log[-10:] if room.get('engine') else [],
    })


# ─────────────────────────────────────────
# SOCKET EVENTS
# ─────────────────────────────────────────

@socketio.on('connect')
def on_connect():
    emit('connected', {'sid': request.sid})


@socketio.on('create_room')
def on_create_room(data):
    """Host creates a new room."""
    data = data or {}
    player_id = data.get('player_id')
    name = (data.get('name') or 'Host').strip()[:20]
    if not player_id:
        emit('error_msg', {'msg': 'Missing player id'})
        return

    code = generate_code()
    while code in rooms:
        code = generate_code()

    sid = request.sid
    rooms[code] = {
        'code': code,
        'host_id': player_id,
        'players': {
            player_id: {
                'name': name,
                'sid': sid,
                'ready': False,
                'alive': True,
                'connected': True,
            }
        },
        'player_order': [player_id],
        'phase': 'lobby',
        'engine': None,
        'roles': {},
        'round': 0,
        'chat_log': [],
        'night_state': {},
        'vote_state': {},
    }

    emit('room_joined', {'code': code}, to=sid)
    broadcast_state(rooms[code])


@socketio.on('join_room_event')
def on_join_room(data):
    """Player joins or rejoins an existing room."""
    data = data or {}
    code = normalize_code(data.get('code'))
    player_id = data.get('player_id')
    name = (data.get('name') or 'Player').strip()[:20]

    if not code or code not in rooms:
        emit('error_msg', {'msg': 'Room not found'})
        return

    room = rooms[code]
    sid = request.sid

    if player_id and player_id in room['players']:
        # Allow existing player to reconnect/bind new socket ID even after redirect
        room['players'][player_id]['sid'] = sid
        room['players'][player_id]['connected'] = True
        if name:
            room['players'][player_id]['name'] = name
        emit('room_joined', {'code': code}, to=sid)
        broadcast_state(room)
        return

    if room['phase'] != 'lobby':
        emit('error_msg', {'msg': 'Game already started'})
        return

    if not player_id:
        emit('error_msg', {'msg': 'Missing player id'})
        return

    if len(room['players']) >= 18:
        emit('error_msg', {'msg': 'Room full'})
        return

    room['players'][player_id] = {
        'name': name,
        'sid': sid,
        'ready': False,
        'alive': True,
        'connected': True,
    }
    room['player_order'].append(player_id)

    emit('room_joined', {'code': code}, to=sid)
    broadcast_state(room)


@socketio.on('toggle_ready')
def on_toggle_ready(data):
    data = data or {}
    code = normalize_code(data.get('code'))
    player_id = data.get('player_id')
    room = rooms.get(code)
    if not room or player_id not in room['players']:
        return
    room['players'][player_id]['ready'] = not room['players'][player_id]['ready']
    broadcast_state(room)


@socketio.on('start_game')
def on_start_game(data):
    data = data or {}
    code = normalize_code(data.get('code'))
    player_id = data.get('player_id')
    room = rooms.get(code)

    if not room or room['host_id'] != player_id:
        emit('error_msg', {'msg': 'Only host can start'})
        return
    if room['phase'] != 'lobby':
        return

    n = len(room['players'])
    if n < 6:
        emit('error_msg', {'msg': 'Need at least 6 players'})
        return

    wolves = max(1, n // 4)
    role_list = ['werewolf'] * wolves + ['seer', 'doctor']
    if n >= 7:
        role_list.append('witch')
    if n >= 9:
        role_list.append('sheriff')
    role_list += ['villager'] * (n - len(role_list))
    random.shuffle(role_list)

    players = [room['players'][pid]['name'] for pid in room['player_order']]
    room['roles'] = {pid: role_list[i] for i, pid in enumerate(room['player_order'])}
    room['engine'] = GameEngine(players, role_list)
    room['round'] = 0
    room['phase'] = 'night_starting'

    broadcast_state(room)

    for pid in room['player_order']:
        p = room['players'][pid]
        if p.get('sid'):
            emit('your_role', {'role': room['roles'][pid]}, to=p['sid'])

    socketio.sleep(3)
    start_night(room)


@socketio.on('submit_action')
def on_submit_action(data):
    """Player submits night action (werewolf kill, seer inspect, etc.)."""
    data = data or {}
    code = normalize_code(data.get('code'))
    player_id = data.get('player_id')
    target_id = data.get('target_id')
    room = rooms.get(code)

    if not room or room['phase'] != 'night':
        return

    ns = room['night_state']
    role = ns.get('current_role')
    if player_id not in role_holders(room, role):
        return

    engine = room['engine']
    actions = ns['actions']

    if role == 'werewolf':
        if target_id in room['player_order']:
            actions['werewolf_kill'] = room['player_order'].index(target_id)
    elif role == 'sheriff':
        if target_id == 'skip':
            actions['sheriff_kill'] = None
        elif target_id in room['player_order']:
            actions['sheriff_kill'] = room['player_order'].index(target_id)
    elif role == 'doctor':
        if target_id in room['player_order']:
            actions['doctor_save'] = room['player_order'].index(target_id)
    elif role == 'seer':
        if target_id in room['players'] and player_id in room['players'] and room['players'][player_id].get('sid'):
            emit('seer_result', {
                'name': room['players'][target_id]['name'],
                'role': room['roles'].get(target_id),
            }, to=room['players'][player_id]['sid'])
    elif role == 'witch':
        if actions.get('witch_poison_used_this_night'):
            return
        if target_id != 'skip' and target_id in room['player_order'] and not engine.witch_poison_used:
            actions['witch_poison'] = room['player_order'].index(target_id)
            actions['witch_poison_used_this_night'] = True
            engine.witch_poison_used = True

    ns['pending'].discard(player_id)
    advance_night(room)


@socketio.on('cast_vote')
def on_cast_vote(data):
    data = data or {}
    code = normalize_code(data.get('code'))
    player_id = data.get('player_id')
    target_id = data.get('target_id')
    room = rooms.get(code)

    if not room or room['phase'] != 'vote':
        return

    vs = room['vote_state']
    if player_id not in vs['pending']:
        return

    vs['votes'][player_id] = target_id if target_id != 'skip' else None
    vs['pending'].discard(player_id)

    if not vs['pending']:
        resolve_vote(room)


@socketio.on('chat_message')
def on_chat(data):
    data = data or {}
    code = normalize_code(data.get('code'))
    player_id = data.get('player_id')
    text = (data.get('text') or '').strip()[:200]
    room = rooms.get(code)

    if not room or not text or player_id not in room['players']:
        return

    p = room['players'][player_id]
    if not p.get('alive', True):
        return

    entry = {
        'from': p['name'],
        'text': text,
        'phase': room['phase'],
        'ts': time.time(),
    }

    if room['phase'] == 'night':
        if room.get('roles', {}).get(player_id) != 'werewolf':
            return
        for pid in role_holders(room, 'werewolf'):
            if room['players'][pid].get('sid'):
                emit('chat', entry, to=room['players'][pid]['sid'])
    else:
        broadcast_to_all(room, 'chat', entry)


@socketio.on('next_phase')
def on_next_phase(data):
    """Host advances from day to vote."""
    data = data or {}
    code = normalize_code(data.get('code'))
    player_id = data.get('player_id')
    room = rooms.get(code)

    if not room or room['host_id'] != player_id:
        return
    if room['phase'] == 'day':
        start_vote(room)


@socketio.on('leave_room_event')
def on_leave(data):
    data = data or {}
    code = normalize_code(data.get('code'))
    player_id = data.get('player_id')
    room = rooms.get(code)
    if not room or player_id not in room['players']:
        return

    if room['phase'] == 'lobby':
        del room['players'][player_id]
        if player_id in room['player_order']:
            room['player_order'].remove(player_id)
        if player_id == room['host_id'] and room['player_order']:
            room['host_id'] = room['player_order'][0]
        if not room['players']:
            del rooms[code]
        else:
            broadcast_state(room)
    else:
        room['players'][player_id]['sid'] = None
        room['players'][player_id]['connected'] = False
        broadcast_state(room)


@socketio.on('disconnect')
def on_disconnect():
    sid = request.sid
    code = find_room_by_sid(sid)
    if not code:
        return
    room = rooms.get(code)
    if not room:
        return
    for p in room['players'].values():
        if p.get('sid') == sid:
            p['sid'] = None
            p['connected'] = False
            break
    broadcast_state(room)