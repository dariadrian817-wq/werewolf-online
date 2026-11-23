"""Real-time online multiplayer — SocketIO handlers."""
from flask_socketio import SocketIO, emit, join_room as sio_join  # pyright: ignore[reportMissingModuleSource]
import random
import time
from game_engine.engine import GameEngine

socketio = SocketIO(cors_allowed_origins="*")

rooms = {}

# ─────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────

def generate_code():
    chars = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'   # no confusing chars
    return ''.join(random.choices(chars, k=6))


def find_room_by_sid(sid):
    for code, room in rooms.items():
        for pid, p in room['players'].items():
            if p.get('sid') == sid:
                return code
    return None


def role_holders(room, role):
    """Return list of player_ids with given role, alive."""
    engine = room.get('engine')
    if not engine:
        return []
    return [
        pid for pid in room['player_order']
        if pid in room['players']
        and room['players'][pid]['alive']
        and room['roles'].get(pid) == role
    ]


def build_public_state(room, viewer_pid=None):
    """State safe for everyone (or one viewer)."""
    engine = room.get('engine')
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

    # Include viewer's own role only
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
    engine = room['engine']

    if ns['step'] >= len(ns['queue']):
        resolve_night(room)
        return

    role = ns['queue'][ns['step']]
    holders = role_holders(room, role)

    if not holders:
        # Dead role — skip (privacy handled separately)
        advance_night(room)
        return

    ns['current_role'] = role
    ns['pending'] = set(holders)   # all holders must act (or one does for group)

    # For werewolf/sheriff, only ONE needs to submit (representative)
    # For witch/doctor/seer (single role), only that player.
    # Simple MVP: any holder can submit; when one submits, move on.

    broadcast_state(room)

    # Tell holders it's their turn (private event)
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
                        and room['players'][tpid]['alive']
                        and not (role == 'werewolf' and room['roles'].get(tpid) == 'werewolf')
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

    # Reset night state
    room['night_state'] = {
        'queue': [], 'step': 0, 'actions': {},
        'current_role': None, 'pending': set()
    }

    # Broadcast deaths
    broadcast_to_all(room, 'night_resolved', {
        'killed': [
            {'name': room['players'][pid]['name'], 'role': room['roles'][pid]}
            for pid in killed_pids
        ],
        'round': room['round'],
    })

    winner = engine.check_win()
    if winner:
        end_game(room, winner)
        return

    # Move to day
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
        'pending': set()
    }
    start_night_step(room)


def start_vote(room):
    """Begin voting phase."""
    room['phase'] = 'vote'
    room['vote_state'] = {
        'votes': {},
        'pending': set(
            pid for pid in room['player_order']
            if pid in room['players'] and room['players'][pid]['alive']
        ),
    }
    broadcast_state(room)
    broadcast_to_all(room, 'vote_started', {
        'targets': [
            {'id': pid, 'name': room['players'][pid]['name']}
            for pid in room['player_order']
            if pid in room['players'] and room['players'][pid]['alive']
        ]
    })


def resolve_vote(room):
    vs = room['vote_state']
    votes = vs['votes']

    # Tally
    tally = {}
    for voter, target in votes.items():
        if target:
            tally[target] = tally.get(target, 0) + 1

    eliminated_pid = None
    if tally:
        max_votes = max(tally.values())
        candidates = [t for t, c in tally.items() if c == max_votes]
        if len(candidates) == 1:
            eliminated_pid = candidates[0]
        # Tie → no elimination

    eliminated_name = None
    eliminated_role = None
    if eliminated_pid:
        eliminated_name = room['players'][eliminated_pid]['name']
        eliminated_role = room['roles'][eliminated_pid]
        room['players'][eliminated_pid]['alive'] = False
        # Update engine alive list
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
        },
    })

    winner = room['engine'].check_win()
    if winner:
        end_game(room, winner)
        return

    # Next night
    start_night(room)


def end_game(room, winner):
    room['phase'] = 'ended'
    room['winner'] = winner
    all_roles = [
        {
            'name': room['players'][pid]['name'],
            'role': room['roles'][pid],
            'alive': room['players'][pid]['alive'],
        }
        for pid in room['player_order']
        if pid in room['players']
    ]
    broadcast_to_all(room, 'game_over', {
        'winner': winner,
        'roles': all_roles,
        'log': room['engine'].log[-10:] if room['engine'] else [],
    })


# ─────────────────────────────────────────
# SOCKET EVENTS
# ─────────────────────────────────────────

@socketio.on('connect')
def on_connect():
    emit('connected', {'sid': 'ok'})


@socketio.on('create_room')
def on_create_room(data):
    """Host creates a new room."""
    player_id = data.get('player_id')
    name = (data.get('name') or 'Host').strip()[:20]
    if not player_id:
        emit('error_msg', {'msg': 'Missing player id'})
        return

    code = generate_code()
    while code in rooms:
        code = generate_code()

    rooms[code] = {
        'code': code,
        'host_id': player_id,
        'players': {
            player_id: {
                'name': name, 'sid': request_sid(),
                'ready': False, 'alive': True, 'connected': True,
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

    emit('room_joined', {'code': code}, to=request_sid())
    broadcast_state(rooms[code])


@socketio.on('join_room_event')
def on_join_room(data):
    """Player joins existing room."""
    code = (data.get('code') or '').upper().strip()
    player_id = data.get('player_id')
    name = (data.get('name') or 'Player').strip()[:20]

    if code not in rooms:
        emit('error_msg', {'msg': 'Room not found'})
        return

    room = rooms[code]
    if room['phase'] != 'lobby':
        emit('error_msg', {'msg': 'Game already started'})
        return

    if player_id in room['players']:
        # Reconnect
        room['players'][player_id]['sid'] = request_sid()
        room['players'][player_id]['connected'] = True
    elif len(room['players']) >= 18:
        emit('error_msg', {'msg': 'Room full'})
        return
    else:
        room['players'][player_id] = {
            'name': name, 'sid': request_sid(),
            'ready': False, 'alive': True, 'connected': True,
        }
        room['player_order'].append(player_id)

    emit('room_joined', {'code': code}, to=request_sid())
    broadcast_state(room)


@socketio.on('toggle_ready')
def on_toggle_ready(data):
    code = data.get('code')
    player_id = data.get('player_id')
    room = rooms.get(code)
    if not room or player_id not in room['players']:
        return
    room['players'][player_id]['ready'] = not room['players'][player_id]['ready']
    broadcast_state(room)


@socketio.on('start_game')
def on_start_game(data):
    code = data.get('code')
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

    # Assign roles (reuse offline logic)
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

    # Send each player their role privately
    for pid in room['player_order']:
        p = room['players'][pid]
        if p.get('sid'):
            emit('your_role', {
                'role': room['roles'][pid],
            }, to=p['sid'])

    # Brief pause then start night
    socketio.sleep(3)
    start_night(room)


@socketio.on('submit_action')
def on_submit_action(data):
    """Player submits night action (werewolf kill, seer inspect, etc.)."""
    code = data.get('code')
    player_id = data.get('player_id')
    target_id = data.get('target_id')
    room = rooms.get(code)

    if not room or room['phase'] != 'night':
        return

    ns = room['night_state']
    role = ns.get('current_role')
    if role not in role_holders(room, role):
        # Not this player's action
        return

    engine = room['engine']
    actions = ns['actions']

    if role == 'werewolf':
        idx = room['player_order'].index(target_id)
        actions['werewolf_kill'] = idx
    elif role == 'sheriff':
        if target_id == 'skip':
            actions['sheriff_kill'] = None
        else:
            idx = room['player_order'].index(target_id)
            actions['sheriff_kill'] = idx
    elif role == 'doctor':
        idx = room['player_order'].index(target_id)
        actions['doctor_save'] = idx
    elif role == 'seer':
        idx = room['player_order'].index(target_id)
        target_name = room['players'][target_id]['name']
        target_role = room['roles'][target_id]
        emit('seer_result', {
            'name': target_name, 'role': target_role
        }, to=room['players'][player_id]['sid'])
        # Seer doesn't affect game state, just their info
    elif role == 'witch':
        if actions.get('witch_poison_used_this_night'):
            return  # already acted
        idx = room['player_order'].index(target_id) if target_id != 'skip' else None
        if idx is not None:
            actions['witch_poison'] = idx
            engine.witch_poison_used = True

    ns['pending'].discard(player_id)
    advance_night(room)


@socketio.on('cast_vote')
def on_cast_vote(data):
    code = data.get('code')
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
    code = data.get('code')
    player_id = data.get('player_id')
    text = (data.get('text') or '').strip()[:200]
    room = rooms.get(code)

    if not room or not text or player_id not in room['players']:
        return

    p = room['players'][player_id]
    if not p['alive']:
        return   # dead can't chat

    entry = {
        'from': p['name'],
        'text': text,
        'phase': room['phase'],
        'ts': time.time(),
    }

    # Night = werewolves only
    if room['phase'] == 'night':
        if room['roles'].get(player_id) != 'werewolf':
            return  # non-wolves can't chat at night
        for pid in role_holders(room, 'werewolf'):
            if room['players'][pid].get('sid'):
                emit('chat', entry, to=room['players'][pid]['sid'])
    else:
        broadcast_to_all(room, 'chat', entry)


@socketio.on('next_phase')
def on_next_phase(data):
    """Host advances from day → vote."""
    code = data.get('code')
    player_id = data.get('player_id')
    room = rooms.get(code)

    if not room or room['host_id'] != player_id:
        return
    if room['phase'] == 'day':
        start_vote(room)


@socketio.on('leave_room_event')
def on_leave(data):
    code = data.get('code')
    player_id = data.get('player_id')
    room = rooms.get(code)
    if not room or player_id not in room['players']:
        return

    if room['phase'] == 'lobby':
        del room['players'][player_id]
        room['player_order'].remove(player_id)
        if player_id == room['host_id'] and room['player_order']:
            room['host_id'] = room['player_order'][0]
        if not room['players']:
            del rooms[code]
        else:
            broadcast_state(room)
    else:
        # Mid-game: mark disconnected
        room['players'][player_id]['sid'] = None
        room['players'][player_id]['connected'] = False
        broadcast_state(room)


@socketio.on('disconnect')
def on_disconnect():
    code = find_room_by_sid(request_sid())
    if not code:
        return
    room = rooms.get(code)
    if not room:
        return
    for pid, p in room['players'].items():
        if p.get('sid') == request_sid():
            p['sid'] = None
            p['connected'] = False
            break
    broadcast_state(room)


# Request sid helper (flask-socketio 5.x)
def request_sid():
    from flask import request
    return request.sid