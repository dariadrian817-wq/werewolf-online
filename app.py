import importlib
try:
    importlib.import_module('gevent.monkey').patch_all()
except ImportError:
    pass
from flask import Flask, render_template, request, session, redirect, url_for
import random
import string
from game_engine.engine import GameEngine
from online.sockets import socketio

app = Flask(__name__)
import os
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-change-later')

# ---------- IN-MEMORY STORE ----------
rooms = {}   # {room_code: {'players': [...], 'mode': 'online'}}

# ---------- HELPERS ----------

def generate_room_code():
    return ''.join(random.choices(string.ascii_uppercase, k=6))

def assign_roles(n):
    """Werewolves = n//4. Witch sa 7+. Sheriff sa 9+."""
    wolves = max(1, n // 4)
    roles = ['werewolf'] * wolves
    roles += ['seer', 'doctor']

    if n >= 7:
        roles += ['witch']
    if n >= 9:
        roles += ['sheriff']

    roles += ['villager'] * (n - len(roles))
    random.shuffle(roles)
    return roles

# ---------- ROUTES: HOME ----------

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/rules')
def rules():
    return render_template('rules.html')

# ---------- ROUTES: OFFLINE ----------

@app.route('/offline/setup', methods=['GET', 'POST'])
def offline_setup():
    if request.method == 'POST':
        names = request.form.getlist('player_name')
        names = [n.strip() for n in names if n.strip()]
        if len(names) < 6:
            return render_template('offline_setup.html',
                                   error="Kailangan ng at least 6 players.",
                                   count=6)
        if len(names) > 18:
            return render_template('offline_setup.html',
                                   error="Max 18 players lang.",
                                   count=6)
        # ✅ Reject duplicates
        if len(names) != len(set(names)):
            return render_template('offline_setup.html',
                               error="Duplicate names — each player needs unique name.",
                               count=6)
        session['players'] = names
        session['roles'] = assign_roles(len(names))
        return redirect(url_for('offline_reveal', idx=0))
    return render_template('offline_setup.html', count=6)

@app.route('/offline/reveal/<int:idx>')
def offline_reveal(idx):
    players = session.get('players', [])
    roles = session.get('roles', [])
    if idx >= len(players):
        return redirect(url_for('offline_done'))
    return render_template('offline_reveal.html',
                           idx=idx,
                           name=players[idx],
                           role=roles[idx],
                           total=len(players))

@app.route('/offline/done')
def offline_done():
    roles = session.get('roles', [])
    counts = {}
    for r in roles:
        counts[r] = counts.get(r, 0) + 1
    return render_template('offline_done.html', counts=counts)

# ---------- ROUTES: ONLINE (stub) ----------

@app.route('/online')
@app.route('/online/join')       # ← alias, para hindi mag-404
@app.route('/online/join/')
def online_index():
    return render_template('online_join.html')

@app.route('/online/room/<code>')
def online_room(code):
    """Room page — handles both lobby and game."""
    return render_template('online_game.html', code=code)
# ---------- GAME HELPERS ----------

def save_game(engine):
    session['game'] = engine.to_dict()

def load_game():
    data = session.get('game')
    if not data:
        return None
    return GameEngine.from_dict(data)

def get_night_queue(engine):
    """Order matters: wolf → sheriff → witch → doctor → seer."""
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
    return queue


# ---------- START GAME ----------

@app.route('/offline/start', methods=['POST'])
def offline_start():
    players = session.get('players', [])
    roles = session.get('roles', [])

    if not players or not roles:
        return redirect(url_for('offline_setup'))

    engine = GameEngine(players, roles)
    save_game(engine)

    session['round'] = 1
    session['night_step'] = 0
    session['night_actions'] = {}
    session['witch_step'] = 'heal'
    return redirect(url_for('offline_night_start'))
# ---------- NIGHT: START (router) ----------

@app.route('/offline/night/start')
def offline_night_start():
    engine = load_game()
    if not engine:
        return redirect(url_for('offline_setup'))

    step = session.get('night_step', 0)
    queue = get_night_queue(engine)

    if step >= len(queue):
        return redirect(url_for('offline_night_resolve'))

    next_role = queue[step]

    # Check kung alive ang role holder
    role_holder_idx = engine.find_role_index(next_role)

    if role_holder_idx is None:
        # Dead role — show no-action screen (privacy fix)
        return redirect(url_for('offline_night_no_action', role=next_role))

    # Normal flow
    return render_template('offline_night_start.html',
                           role=next_role,
                           round=session.get('round', 1))
# ---------- NIGHT: WEREWOLF ----------

@app.route('/offline/night/werewolf', methods=['GET', 'POST'])
def offline_night_werewolf():
    engine = load_game()
    if not engine:
        return redirect(url_for('offline_setup'))

    wolves = [engine.players[i] for i in engine.alive_werewolves()]
    wolf_indices = engine.alive_werewolves()

    if request.method == 'POST':
        target = request.form.get('target', '')
        if not target:
            return redirect(url_for('offline_night_werewolf'))
        target_idx = int(target)

        # Server-side validation: bawal patayin ang kapwa werewolf
        if target_idx in wolf_indices:
            return redirect(url_for('offline_night_werewolf'))

        actions = session.get('night_actions', {})
        actions['werewolf_kill'] = target_idx
        session['night_actions'] = actions
        session['night_step'] = session.get('night_step', 0) + 1
        return redirect(url_for('offline_night_close', role='werewolf'))

    # ✅ Exclude wolves from target list
    targets = [{'index': i, 'name': engine.players[i]}
               for i in engine.alive_indices() if i not in wolf_indices]
    return render_template('offline_night_werewolf.html',
                           wolves=wolves, targets=targets,
                           round=session.get('round', 1))

# ---------- NIGHT: SHERIFF ----------

@app.route('/offline/night/sheriff', methods=['GET', 'POST'])
def offline_night_sheriff():
    engine = load_game()
    if not engine:
        return redirect(url_for('offline_setup'))

    sheriff_idx = engine.find_role_index('sheriff')
    sheriff_name = engine.players[sheriff_idx] if sheriff_idx is not None else 'Sheriff'

    if request.method == 'POST':
        target = request.form.get('target', 'skip')
        actions = session.get('night_actions', {})
        if target == 'skip':
            actions['sheriff_kill'] = None
        else:
            actions['sheriff_kill'] = int(target)
        session['night_actions'] = actions
        session['night_step'] = session.get('night_step', 0) + 1
        return redirect(url_for('offline_night_close', role='sheriff'))   # o 'sheriff', 'witch', etc.

    targets = [{'index': i, 'name': engine.players[i]}
               for i in engine.alive_indices() if i != sheriff_idx]
    return render_template('offline_night_sheriff.html',
                           sheriff_name=sheriff_name, targets=targets,
                           round=session.get('round', 1))
# ---------- NIGHT: WITCH (2 steps) ----------

@app.route('/offline/night/witch', methods=['GET', 'POST'])
def offline_night_witch():
    engine = load_game()
    if not engine:
        return redirect(url_for('offline_setup'))

    witch_idx = engine.find_role_index('witch')
    witch_name = engine.players[witch_idx] if witch_idx is not None else 'Witch'

    actions = session.get('night_actions', {})
    wolf_target = actions.get('werewolf_kill')
    wolf_target_name = engine.players[wolf_target] if wolf_target is not None else None

    step = session.get('witch_step', 'heal')

    if request.method == 'POST':
        # HEAL STEP
        if step == 'heal':
            if request.form.get('heal_decision') == 'yes' and not engine.witch_heal_used:
                actions = session.get('night_actions', {})
                actions['witch_heal'] = wolf_target
                session['night_actions'] = actions
                engine.witch_heal_used = True
                save_game(engine)
            session['witch_step'] = 'poison'
            return redirect(url_for('offline_night_witch'))

        # POISON STEP
        if step == 'poison':
            poison = request.form.get('poison_decision')
            if poison and poison != 'skip' and not engine.witch_poison_used:
                actions = session.get('night_actions', {})
                actions['witch_poison'] = int(poison)
                session['night_actions'] = actions
                engine.witch_poison_used = True
                save_game(engine)
            session['witch_step'] = 'done'
            session['night_step'] = session.get('night_step', 0) + 1
            return redirect(url_for('offline_night_close', role='witch'))   # o 'sheriff', 'witch', etc.

    targets = [{'index': i, 'name': engine.players[i]}
               for i in engine.alive_indices() if i != witch_idx]

    return render_template('offline_night_witch.html',
                           witch_name=witch_name,
                           wolf_target_name=wolf_target_name,
                           heal_available=not engine.witch_heal_used,
                           poison_available=not engine.witch_poison_used,
                           step=step, targets=targets,
                           round=session.get('round', 1))

# ---------- NIGHT: DOCTOR ----------

@app.route('/offline/night/doctor', methods=['GET', 'POST'])
def offline_night_doctor():
    engine = load_game()
    if not engine:
        return redirect(url_for('offline_setup'))

    doctor_idx = engine.find_role_index('doctor')
    doctor_name = engine.players[doctor_idx] if doctor_idx is not None else 'Doctor'

    if request.method == 'POST':
        target = int(request.form['target'])
        actions = session.get('night_actions', {})
        actions['doctor_save'] = target
        session['night_actions'] = actions
        session['night_step'] = session.get('night_step', 0) + 1
        return redirect(url_for('offline_night_close', role='doctor'))   # o 'sheriff', 'witch', etc.

    targets = [{'index': i, 'name': engine.players[i]}
               for i in engine.alive_indices()]
    return render_template('offline_night_doctor.html',
                           doctor_name=doctor_name, targets=targets,
                           round=session.get('round', 1))

# ---------- NIGHT: SEER ----------

@app.route('/offline/night/seer', methods=['GET', 'POST'])
def offline_night_seer():
    engine = load_game()
    if not engine:
        return redirect(url_for('offline_setup'))

    seer_idx = engine.find_role_index('seer')
    seer_name = engine.players[seer_idx] if seer_idx is not None else 'Seer'

    if request.method == 'POST':
        if 'inspect' in request.form:
            session.pop('seer_result', None)   # ✅ clear old
            target = int(request.form['inspect'])
            session['seer_result'] = {
                'name': engine.players[target],
                'role': engine.roles[target],
           }
        if 'confirm' in request.form:
            session.pop('seer_result', None)
            session['night_step'] = session.get('night_step', 0) + 1
            return redirect(url_for('offline_night_close', role='seer'))   # o 'sheriff', 'witch', etc.

    result = session.get('seer_result')
    targets = [{'index': i, 'name': engine.players[i]}
               for i in engine.alive_indices()]
    return render_template('offline_night_seer.html',
                           seer_name=seer_name, targets=targets,
                           result=result, round=session.get('round', 1))
@app.route('/offline/night/resolve')
def offline_night_resolve():
    engine = load_game()
    if not engine:
        return redirect(url_for('offline_setup'))

    actions = session.get('night_actions', {})
    killed_indices = engine.resolve_night_actions(actions)
    killed_names = [engine.players[i] for i in killed_indices]

    engine.log.append({
            'round': session.get('round', 1),
            'type': 'night',
        'killed': killed_names,
    })

    # ✅ Trim log (prevents 4KB cookie overflow)
    if len(engine.log) > 10:
        engine.log = engine.log[-10:]

    session['night_actions'] = {}
    session['night_step'] = 0
    session['killed_last_night'] = killed_names

    # ✅ CHECK WIN FIRST, then save (para ma-persist ang winner)
    winner = engine.check_win()
    save_game(engine)

    if winner:
        return redirect(url_for('offline_gameover'))

    return render_template('offline_night_resolve.html',
                           killed=killed_names,
                           round=session.get('round', 1))
# ---------- DAY ----------

@app.route('/offline/day', methods=['GET', 'POST'])
def offline_day():
    engine = load_game()
    if not engine:
        return redirect(url_for('offline_setup'))

    if request.method == 'POST':
        return redirect(url_for('offline_vote'))

    killed = session.pop('killed_last_night', [])
    alive = [{'name': engine.players[i]} for i in engine.alive_indices()]
    return render_template('offline_day.html',
                           killed=killed, alive=alive,
                           round=session.get('round', 1))

# ---------- VOTE ----------

@app.route('/offline/vote', methods=['GET', 'POST'])
def offline_vote():
    engine = load_game()
    if not engine:
        return redirect(url_for('offline_setup'))

    if request.method == 'POST':
        # ── Safe target parsing ──
        target = request.form.get('target', '').strip()
        if not target or target == 'skip':
            target_idx = None
        else:
            try:
                target_idx = int(target)
            except (ValueError, TypeError):
                target_idx = None

        # ── Resolve vote ──
        eliminated_idx = engine.resolve_vote(target_idx)
        eliminated_name = (engine.players[eliminated_idx]
                           if eliminated_idx is not None else None)

        # ── Log ──
        engine.log.append({
            'round': session.get('round', 1),
            'type': 'vote',
            'eliminated': eliminated_name,
        })

        # ── Trim log (session size) ──
        if len(engine.log) > 10:
            engine.log = engine.log[-10:]

        # ── Check win BEFORE save ──
        winner = engine.check_win()
        save_game(engine)

        if winner:
            return redirect(url_for('offline_gameover'))

        # ── Advance round ──
        session['round'] = session.get('round', 1) + 1
        session['night_step'] = 0
        session['night_actions'] = {}
        session['witch_step'] = 'heal'
        return redirect(url_for('offline_night_start'))

    # ── GET — show vote page ──
    targets = [{'index': i, 'name': engine.players[i]}
               for i in engine.alive_indices()]
    return render_template('offline_vote.html',
                           targets=targets,
                           round=session.get('round', 1))
# ---------- GAME OVER ----------

@app.route('/offline/gameover')
def offline_gameover():
    engine = load_game()
    if not engine:
        return redirect(url_for('offline_setup'))

    # 🛡️ SAFETY NET: kung walang winner sa session, i-recheck
    if engine.winner is None:
        engine.check_win()
        save_game(engine)

    all_players = [{'name': engine.players[i],
                    'role': engine.roles[i],
                    'alive': engine.alive[i]}
                   for i in range(len(engine.players))]

    return render_template('offline_gameover.html',
                           winner=engine.winner,
                           players=all_players,
                           log=engine.log)

@app.route('/offline/night/close/<role>')
def offline_night_close(role):
    return render_template('offline_night_close.html',
                           role=role,
                           round=session.get('round', 1))

@app.route('/offline/night/skip')
def offline_night_skip():
    """Skip past a dead role — advance night step without action."""
    engine = load_game()
    if not engine:
        return redirect(url_for('offline_setup'))
    session['night_step'] = session.get('night_step', 0) + 1
    return redirect(url_for('offline_night_start'))



# Payagan ang lahat ng origin (pinakamadali para sa deployment)
socketio.init_app(app, async_mode='gevent')
if __name__ == '__main__':
    import os
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port, debug=False)