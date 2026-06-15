#!/usr/bin/env python3
"""
Enregistreur d'écran avec options avancées d'audio, souris et clavier.
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import threading
import time
import cv2
import numpy as np
import mss
from pynput import mouse as pmouse, keyboard as pkeyboard
import os
import subprocess
import tempfile
import wave
import shutil
from datetime import datetime

# --- Imports Windows optionnels ---
try:
    import win32gui
    HAS_WIN32 = True
except ImportError:
    HAS_WIN32 = False

# --- Bibliothèque audio (pyaudiowpatch pour le loopback WASAPI) ---
try:
    import pyaudiowpatch as pyaudio
    HAS_WASAPI = True
except ImportError:
    try:
        import pyaudio  # type: ignore
        HAS_WASAPI = False
    except ImportError:
        pyaudio = None
        HAS_WASAPI = False

# === Constantes ===
AUDIO_RATE = 44100
AUDIO_CHANNELS = 2
AUDIO_CHUNK = 1024
TARGET_FPS = 30

# Forme du curseur flèche (polygone relatif à l'origine)
_CURSOR_PTS = np.array([
    [0, 0], [0, 15], [4, 11], [7, 18],
    [10, 17], [7, 10], [13, 10]
], dtype=np.int32)


# =============================================================================
# MouseTracker
# =============================================================================

class MouseTracker:
    """Suit la position et les clics de la souris."""

    def __init__(self):
        self.position = (0, 0)
        self._left_until = 0.0   # timestamp jusqu'auquel le clic gauche est actif
        self._right_until = 0.0
        self._listener = None
        self._lock = threading.Lock()

    def start(self):
        self._listener = pmouse.Listener(
            on_move=self._on_move,
            on_click=self._on_click
        )
        self._listener.daemon = True
        self._listener.start()

    def stop(self):
        if self._listener:
            self._listener.stop()

    def _on_move(self, x, y):
        with self._lock:
            self.position = (x, y)

    def _on_click(self, x, y, button, pressed):
        now = time.time()
        with self._lock:
            self.position = (x, y)
            if button == pmouse.Button.left:
                # Maintenu pendant le clic, +1s après relâchement
                self._left_until = float('inf') if pressed else now + 1.0
            elif button == pmouse.Button.right:
                self._right_until = float('inf') if pressed else now + 1.0

    def _get_state(self):
        with self._lock:
            now = time.time()
            return (
                self.position,
                now < self._left_until,
                now < self._right_until,
            )

    def draw_on_frame(self, frame, mode, monitor_offset=(0, 0)):
        if mode == 'hidden':
            return frame

        pos, left_active, right_active = self._get_state()
        mx, my = pos
        ox, oy = monitor_offset
        px, py = int(mx - ox), int(my - oy)

        h, w = frame.shape[:2]
        if not (-30 <= px < w + 30 and -30 <= py < h + 30):
            return frame

        if mode == 'normal':
            return self._draw_arrow(frame, px, py)
        elif mode == 'clicks':
            return self._draw_click_widget(frame, px, py, left_active, right_active)
        return frame

    def _draw_arrow(self, frame, x, y):
        pts = _CURSOR_PTS + np.array([[x, y]])
        h, w = frame.shape[:2]
        pts = np.clip(pts, 0, [w - 1, h - 1])
        cv2.fillPoly(frame, [pts], (255, 255, 255))
        cv2.polylines(frame, [pts], True, (0, 0, 0), 1)
        return frame

    def _draw_click_widget(self, frame, x, y, left_active, right_active):
        lw, mw, rw = 20, 12, 20
        btn_h = 42
        total_w = lw + mw + rw
        h, w = frame.shape[:2]

        # Positionner à droite/dessous du curseur, avec correction de bord
        wx = min(x + 22, w - total_w - 4)
        wy = min(y + 12, h - btn_h - 4)
        wx = max(wx, 0)
        wy = max(wy, 0)

        # Couleurs BGR
        c_active = (0, 0, 220)
        c_idle = (170, 170, 170)
        c_wheel_bg = (95, 95, 95)
        c_wheel = (55, 55, 55)
        c_shadow = (25, 25, 25)
        c_border = (10, 10, 10)

        # Ombre
        cv2.rectangle(frame, (wx + 2, wy + 2),
                      (wx + total_w + 2, wy + btn_h + 2), c_shadow, -1)

        # Bouton gauche
        cv2.rectangle(frame, (wx, wy),
                      (wx + lw, wy + btn_h), c_active if left_active else c_idle, -1)

        # Zone molette
        cv2.rectangle(frame, (wx + lw, wy),
                      (wx + lw + mw, wy + btn_h), c_wheel_bg, -1)
        wh_top = wy + btn_h // 4
        wh_bot = wy + 3 * btn_h // 4
        cx_w = wx + lw + mw // 2
        cy_w = (wh_top + wh_bot) // 2
        cv2.ellipse(frame, (cx_w, cy_w),
                    (mw // 2 - 2, (wh_bot - wh_top) // 2),
                    0, 0, 360, c_wheel, -1)

        # Bouton droit
        cv2.rectangle(frame, (wx + lw + mw, wy),
                      (wx + total_w, wy + btn_h), c_active if right_active else c_idle, -1)

        # Bordures
        cv2.rectangle(frame, (wx, wy), (wx + total_w, wy + btn_h), c_border, 1)
        cv2.line(frame, (wx + lw, wy), (wx + lw, wy + btn_h), c_border, 1)
        cv2.line(frame, (wx + lw + mw, wy), (wx + lw + mw, wy + btn_h), c_border, 1)

        # Point curseur
        cv2.circle(frame, (x, y), 4, (255, 255, 255), -1)
        cv2.circle(frame, (x, y), 4, (0, 0, 0), 1)

        return frame


# =============================================================================
# KeyboardTracker
# =============================================================================

class KeyboardTracker:
    """Suit les frappes clavier et les affiche en sous-titre."""

    def __init__(self):
        self._buf = []           # liste de (timestamp, char)
        self._lock = threading.Lock()
        self._current_win = None
        self._listener = None
        self._running = False

    def start(self):
        self._running = True
        self._listener = pkeyboard.Listener(on_press=self._on_press)
        self._listener.daemon = True
        self._listener.start()

        if HAS_WIN32:
            t = threading.Thread(target=self._watch_window, daemon=True)
            t.start()

    def stop(self):
        self._running = False
        if self._listener:
            self._listener.stop()

    def _watch_window(self):
        """Efface le buffer quand la fenêtre active change."""
        while self._running:
            try:
                hwnd = win32gui.GetForegroundWindow()
                if hwnd != self._current_win:
                    self._current_win = hwnd
                    with self._lock:
                        self._buf.clear()
            except Exception:
                pass
            time.sleep(0.25)

    def _on_press(self, key):
        try:
            if key == pkeyboard.Key.backspace:
                with self._lock:
                    if self._buf:
                        self._buf.pop()
                return

            if hasattr(key, 'char') and key.char:
                text = key.char
            elif key == pkeyboard.Key.space:
                text = ' '
            elif key == pkeyboard.Key.enter:
                text = ' ↵ '
            elif key == pkeyboard.Key.tab:
                text = ' → '
            else:
                return  # Ignorer les touches de contrôle

            with self._lock:
                now = time.time()
                self._buf.append((now, text))
                # Conserver uniquement les 10 dernières secondes
                cutoff = now - 10.0
                self._buf = [(t, c) for t, c in self._buf if t >= cutoff]
        except Exception:
            pass

    def get_text(self):
        with self._lock:
            cutoff = time.time() - 10.0
            return ''.join(c for t, c in self._buf if t >= cutoff)

    def draw_on_frame(self, frame):
        text = self.get_text()
        if not text:
            return frame

        h, w = frame.shape[:2]
        font = cv2.FONT_HERSHEY_SIMPLEX
        scale = 0.68
        thickness = 2

        # Tronquer si trop long
        if len(text) > 85:
            text = '…' + text[-84:]

        (tw, th), baseline = cv2.getTextSize(text, font, scale, thickness)
        bar_h = th + baseline + 24
        bar_y = h - bar_h

        # Fond semi-transparent
        overlay = frame.copy()
        cv2.rectangle(overlay, (0, bar_y), (w, h), (15, 15, 15), -1)
        cv2.addWeighted(overlay, 0.72, frame, 0.28, 0, frame)

        tx = max(10, (w - tw) // 2)
        ty = h - baseline - 12

        # Ombre puis texte
        cv2.putText(frame, text, (tx + 1, ty + 1),
                    font, scale, (0, 0, 0), thickness + 2)
        cv2.putText(frame, text, (tx, ty),
                    font, scale, (245, 245, 245), thickness)

        return frame


# =============================================================================
# AudioRecorder
# =============================================================================

class AudioRecorder:
    """Enregistre micro et/ou haut-parleur (loopback WASAPI)."""

    def __init__(self, mic_idx, spk_idx):
        self.mic_idx = mic_idx
        self.spk_idx = spk_idx
        self.mic_file = None
        self.spk_file = None
        self._stop = threading.Event()
        self._threads = []

    def start(self):
        self._stop.clear()
        if pyaudio is None:
            return

        if self.mic_idx is not None:
            self.mic_file = tempfile.mktemp(suffix='_mic.wav')
            t = threading.Thread(target=self._record_input,
                                 args=(self.mic_idx, self.mic_file), daemon=True)
            t.start()
            self._threads.append(t)

        if self.spk_idx is not None and HAS_WASAPI:
            self.spk_file = tempfile.mktemp(suffix='_spk.wav')
            t = threading.Thread(target=self._record_loopback,
                                 args=(self.spk_idx, self.spk_file), daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self):
        self._stop.set()
        for t in self._threads:
            t.join(timeout=5)
        self._threads.clear()

    def _record_input(self, device_idx, filepath):
        pa = pyaudio.PyAudio()
        try:
            stream = pa.open(
                format=pyaudio.paInt16,
                channels=AUDIO_CHANNELS,
                rate=AUDIO_RATE,
                input=True,
                input_device_index=device_idx,
                frames_per_buffer=AUDIO_CHUNK,
            )
            frames = []
            while not self._stop.is_set():
                try:
                    frames.append(stream.read(AUDIO_CHUNK, exception_on_overflow=False))
                except Exception:
                    break
            stream.stop_stream()
            stream.close()
            self._save_wav(filepath, frames, AUDIO_CHANNELS, AUDIO_RATE)
        except Exception as e:
            print(f"[Audio mic] Erreur: {e}")
        finally:
            pa.terminate()

    def _record_loopback(self, output_device_idx, filepath):
        pa = pyaudio.PyAudio()
        try:
            out_info = pa.get_device_info_by_index(output_device_idx)
            # Chercher le device loopback correspondant
            loopback = None
            for lb in pa.get_loopback_device_info_generator():
                if out_info['name'] in lb['name']:
                    loopback = lb
                    break
            if loopback is None:
                for lb in pa.get_loopback_device_info_generator():
                    loopback = lb
                    break
            if loopback is None:
                print("[Audio spk] Aucun device loopback trouvé.")
                return

            channels = min(int(loopback.get('maxInputChannels', 2)), 2)
            rate = int(loopback.get('defaultSampleRate', AUDIO_RATE))

            stream = pa.open(
                format=pyaudio.paInt16,
                channels=channels,
                rate=rate,
                input=True,
                input_device_index=loopback['index'],
                frames_per_buffer=AUDIO_CHUNK,
            )
            frames = []
            while not self._stop.is_set():
                try:
                    frames.append(stream.read(AUDIO_CHUNK, exception_on_overflow=False))
                except Exception:
                    break
            stream.stop_stream()
            stream.close()
            self._save_wav(filepath, frames, channels, rate)
        except Exception as e:
            print(f"[Audio spk] Erreur: {e}")
        finally:
            pa.terminate()

    @staticmethod
    def _save_wav(filepath, frames, channels, rate):
        if not frames:
            return
        with wave.open(filepath, 'wb') as wf:
            wf.setnchannels(channels)
            wf.setsampwidth(2)
            wf.setframerate(rate)
            wf.writeframes(b''.join(frames))

    def get_audio_files(self):
        return [f for f in [self.mic_file, self.spk_file]
                if f and os.path.exists(f)]

    def cleanup(self):
        for f in [self.mic_file, self.spk_file]:
            if f and os.path.exists(f):
                try:
                    os.remove(f)
                except Exception:
                    pass


# =============================================================================
# ScreenRecorder
# =============================================================================

class ScreenRecorder:
    """Coordonne la capture vidéo, audio et les overlays."""

    def __init__(self, config):
        self.config = config
        self._stop_event = threading.Event()
        self._temp_video = None
        self._thread = None

        self.mouse_tracker = MouseTracker()
        self.keyboard_tracker = (KeyboardTracker()
                                  if config.get('show_keys') else None)
        self.audio_recorder = AudioRecorder(
            config.get('mic_idx'),
            config.get('spk_idx'),
        )

    def start_async(self):
        self.mouse_tracker.start()
        if self.keyboard_tracker:
            self.keyboard_tracker.start()
        self.audio_recorder.start()

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._record_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()

    def is_running(self):
        return self._thread is not None and self._thread.is_alive()

    def wait(self):
        if self._thread:
            self._thread.join(timeout=10)
        self.audio_recorder.stop()
        self.mouse_tracker.stop()
        if self.keyboard_tracker:
            self.keyboard_tracker.stop()

    def _record_loop(self):
        mon_info = self.config['monitor_info']
        mouse_mode = self.config['mouse_mode']
        offset = (mon_info['left'], mon_info['top'])

        self._temp_video = tempfile.mktemp(suffix='_screen.avi')

        # Dimensions paires requises par certains codecs
        cap_w = mon_info['width'] & ~1
        cap_h = mon_info['height'] & ~1

        fourcc = cv2.VideoWriter_fourcc(*'XVID')
        out = cv2.VideoWriter(self._temp_video, fourcc, TARGET_FPS, (cap_w, cap_h))

        frame_dur = 1.0 / TARGET_FPS
        monitor = {
            'left': mon_info['left'],
            'top': mon_info['top'],
            'width': cap_w,
            'height': cap_h,
        }

        with mss.MSS() as sct:
            while not self._stop_event.is_set():
                t0 = time.perf_counter()

                img = np.array(sct.grab(monitor))
                frame = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)

                frame = self.mouse_tracker.draw_on_frame(frame, mouse_mode, offset)
                if self.keyboard_tracker:
                    frame = self.keyboard_tracker.draw_on_frame(frame)

                out.write(frame)

                sleep = frame_dur - (time.perf_counter() - t0)
                if sleep > 0:
                    time.sleep(sleep)

        out.release()

    @staticmethod
    def _find_ffmpeg():
        """Retourne le chemin vers ffmpeg : venv (imageio-ffmpeg) > local > système."""
        try:
            import imageio_ffmpeg
            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            pass
        local = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), '.ffmpeg', 'ffmpeg.exe'
        )
        return local if os.path.isfile(local) else 'ffmpeg'

    def save(self, output_path):
        """Encode et sauvegarde la vidéo finale avec ffmpeg."""
        ffmpeg = self._find_ffmpeg()
        audio_files = self.audio_recorder.get_audio_files()

        try:
            if not audio_files:
                cmd = [
                    ffmpeg, '-y', '-i', self._temp_video,
                    '-c:v', 'libx264', '-preset', 'fast', '-crf', '22',
                    output_path,
                ]
            elif len(audio_files) == 1:
                cmd = [
                    ffmpeg, '-y',
                    '-i', self._temp_video,
                    '-i', audio_files[0],
                    '-c:v', 'libx264', '-preset', 'fast', '-crf', '22',
                    '-c:a', 'aac', '-b:a', '192k',
                    '-shortest', output_path,
                ]
            else:
                cmd = [
                    ffmpeg, '-y',
                    '-i', self._temp_video,
                    '-i', audio_files[0],
                    '-i', audio_files[1],
                    '-filter_complex',
                    'amix=inputs=2:duration=first:dropout_transition=2',
                    '-c:v', 'libx264', '-preset', 'fast', '-crf', '22',
                    '-c:a', 'aac', '-b:a', '192k',
                    '-shortest', output_path,
                ]

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if result.returncode != 0:
                raise RuntimeError(result.stderr[-500:])
            return True, None

        except FileNotFoundError:
            # ffmpeg absent : sauvegarder le fichier AVI brut
            avi_path = os.path.splitext(output_path)[0] + '.avi'
            try:
                shutil.copy2(self._temp_video, avi_path)
                return True, (
                    f"ffmpeg introuvable — vidéo sauvegardée sans audio au format AVI :\n{avi_path}"
                )
            except Exception as e2:
                return False, str(e2)
        except Exception as e:
            return False, str(e)
        finally:
            self._cleanup()

    def _cleanup(self):
        if self._temp_video and os.path.exists(self._temp_video):
            try:
                os.remove(self._temp_video)
            except Exception:
                pass
        self.audio_recorder.cleanup()


# =============================================================================
# CountdownOverlay
# =============================================================================

class CountdownOverlay:
    """Fenêtre de décompte affichée sur l'écran à enregistrer."""

    def __init__(self, root, monitor_info, on_done):
        self.on_done = on_done

        w, h = 240, 240
        cx = monitor_info['left'] + monitor_info['width'] // 2 - w // 2
        cy = monitor_info['top'] + monitor_info['height'] // 2 - h // 2

        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes('-topmost', True)
        self.win.geometry(f"{w}x{h}+{cx}+{cy}")
        self.win.configure(bg='black')
        try:
            self.win.attributes('-alpha', 0.88)
        except Exception:
            pass

        self.num_lbl = tk.Label(
            self.win, text='', font=('Arial', 110, 'bold'),
            fg='white', bg='black',
        )
        self.num_lbl.pack(expand=True)

        self.sub_lbl = tk.Label(
            self.win, text='Prêt à enregistrer',
            font=('Arial', 11), fg='#aaaaaa', bg='black',
        )
        self.sub_lbl.pack(pady=(0, 18))

    def start(self, n):
        self._tick(n)

    def _tick(self, n):
        if n > 0:
            self.num_lbl.config(text=str(n), fg='white')
            self.win.after(1000, lambda: self._tick(n - 1))
        else:
            self.num_lbl.config(text='●', fg='#ff3333')
            self.sub_lbl.config(text='Démarrage...')
            self.win.after(600, self._finish)

    def _finish(self):
        try:
            self.win.destroy()
        except Exception:
            pass
        self.on_done()


# =============================================================================
# RecordingWidget  (bouton d'arrêt flottant)
# =============================================================================

class RecordingWidget:
    """Petit panneau flottant avec chrono et bouton d'arrêt."""

    def __init__(self, root, on_stop):
        self.on_stop = on_stop
        self._start = time.time()
        self._running = True

        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes('-topmost', True)
        self.win.configure(bg='#1e1e1e')
        self.win.geometry('+20+20')

        self._build_ui()
        self._update_timer()
        self._blink_tick = 0
        self._blink()

    def _build_ui(self):
        # Barre de titre (poignée de déplacement)
        bar = tk.Frame(self.win, bg='#2a2a2a', cursor='fleur')
        bar.pack(fill='x', ipady=3)
        tk.Label(bar, text='  Enregistrement en cours',
                 font=('Arial', 8), fg='#888888', bg='#2a2a2a').pack(side='left')
        bar.bind('<ButtonPress-1>', self._drag_start)
        bar.bind('<B1-Motion>', self._drag_motion)

        # Indicateur + chrono
        row = tk.Frame(self.win, bg='#1e1e1e')
        row.pack(padx=14, pady=(10, 4))

        self.dot = tk.Label(row, text='●', font=('Arial', 14, 'bold'),
                            fg='#ff3333', bg='#1e1e1e')
        self.dot.pack(side='left')

        self.timer_lbl = tk.Label(row, text='00:00',
                                  font=('Courier', 20, 'bold'),
                                  fg='white', bg='#1e1e1e')
        self.timer_lbl.pack(side='left', padx=(8, 0))

        # Bouton arrêt
        tk.Button(
            self.win, text='■  Arrêter l\'enregistrement',
            font=('Arial', 10, 'bold'),
            bg='#cc0000', fg='white',
            activebackground='#ff2222', activeforeground='white',
            relief='flat', padx=16, pady=8,
            command=self._stop,
        ).pack(padx=14, pady=(4, 14), fill='x')

    def _update_timer(self):
        if not self._running or not self.win.winfo_exists():
            return
        elapsed = int(time.time() - self._start)
        m, s = divmod(elapsed, 60)
        self.timer_lbl.config(text=f'{m:02d}:{s:02d}')
        self.win.after(1000, self._update_timer)

    def _blink(self):
        if not self._running or not self.win.winfo_exists():
            return
        self._blink_tick += 1
        color = '#ff3333' if self._blink_tick % 2 == 0 else '#1e1e1e'
        self.dot.config(fg=color)
        self.win.after(500, self._blink)

    def _drag_start(self, e):
        self._dx = e.x_root - self.win.winfo_x()
        self._dy = e.y_root - self.win.winfo_y()

    def _drag_motion(self, e):
        self.win.geometry(f'+{e.x_root - self._dx}+{e.y_root - self._dy}')

    def _stop(self):
        self.on_stop()

    def close(self):
        self._running = False
        try:
            if self.win and self.win.winfo_exists():
                self.win.destroy()
        except Exception:
            pass


# =============================================================================
# SettingsApp  (fenêtre principale)
# =============================================================================

class SettingsApp:
    """Fenêtre de configuration et orchestrateur principal."""

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Enregistreur d'écran")
        self.root.resizable(False, False)
        self.root.protocol('WM_DELETE_WINDOW', self._on_close)

        self.monitors = []
        self.input_devices = []    # [(label, idx | None), ...]
        self.output_devices = []

        self._recorder = None
        self._rec_widget = None

        self._setup_style()
        self._build_ui()
        self._load_devices()

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def _setup_style(self):
        style = ttk.Style()
        try:
            style.theme_use('clam')
        except Exception:
            pass

    def _load_devices(self):
        # Écrans
        with mss.MSS() as sct:
            self.monitors = list(sct.monitors[1:])  # monitors[0] = tous les écrans

        names = [
            f"Écran {i + 1}  ({m['width']}×{m['height']})"
            for i, m in enumerate(self.monitors)
        ]
        self.monitor_combo['values'] = names
        if names:
            self.monitor_combo.current(0)

        # Périphériques audio
        self.input_devices = [('Aucun', None)]
        self.output_devices = [('Aucun', None)]

        if pyaudio is not None:
            pa = pyaudio.PyAudio()
            try:
                for i in range(pa.get_device_count()):
                    try:
                        info = pa.get_device_info_by_index(i)
                        if info.get('isLoopbackDevice', False):
                            continue
                        name = info.get('name', f'Device {i}')
                        if info.get('maxInputChannels', 0) > 0:
                            self.input_devices.append((name, i))
                        if info.get('maxOutputChannels', 0) > 0:
                            self.output_devices.append((name, i))
                    except Exception:
                        pass
            finally:
                pa.terminate()

        self.mic_combo['values'] = [d[0] for d in self.input_devices]
        self.spk_combo['values'] = [d[0] for d in self.output_devices]
        self.mic_combo.current(0)
        self.spk_combo.current(0)

        if not HAS_WASAPI:
            self.spk_combo.config(state='disabled')
            self.spk_note.config(text='⚠ Installer pyaudiowpatch pour capturer le son système')

    # ------------------------------------------------------------------
    # Construction de l'interface
    # ------------------------------------------------------------------

    def _build_ui(self):
        root = self.root
        root.configure(bg='#f0f0f0')
        PAD = 14

        # En-tête
        tk.Label(root, text="🎬  Enregistreur d'écran",
                 font=('Arial', 14, 'bold'), bg='#f0f0f0').pack(
            pady=(PAD, 6), padx=PAD * 2)
        ttk.Separator(root).pack(fill='x', padx=PAD)

        def section_header(text):
            f = tk.Frame(root, bg='#f0f0f0')
            f.pack(fill='x', padx=PAD, pady=(10, 0))
            tk.Label(f, text=text, font=('Arial', 8, 'bold'),
                     fg='#666666', bg='#f0f0f0').pack(anchor='w')
            ttk.Separator(root).pack(fill='x', padx=PAD, pady=(2, 6))

        # ── Écran ─────────────────────────────────────────────────────
        section_header('ÉCRAN À ENREGISTRER')
        f = tk.Frame(root, bg='#f0f0f0')
        f.pack(fill='x', padx=PAD * 2, pady=(0, 4))
        tk.Label(f, text='Moniteur :', bg='#f0f0f0').grid(
            row=0, column=0, sticky='w')
        self.monitor_combo = ttk.Combobox(f, state='readonly', width=32)
        self.monitor_combo.grid(row=0, column=1, padx=(10, 0))

        # ── Audio ──────────────────────────────────────────────────────
        section_header('AUDIO')
        f = tk.Frame(root, bg='#f0f0f0')
        f.pack(fill='x', padx=PAD * 2, pady=(0, 4))

        tk.Label(f, text='Microphone :', bg='#f0f0f0').grid(
            row=0, column=0, sticky='w', pady=3)
        self.mic_combo = ttk.Combobox(f, state='readonly', width=32)
        self.mic_combo.grid(row=0, column=1, padx=(10, 0), pady=3)

        tk.Label(f, text='Haut-parleur :', bg='#f0f0f0').grid(
            row=1, column=0, sticky='w', pady=3)
        self.spk_combo = ttk.Combobox(f, state='readonly', width=32)
        self.spk_combo.grid(row=1, column=1, padx=(10, 0), pady=3)

        self.spk_note = tk.Label(f, text='', font=('Arial', 8),
                                 fg='#e07000', bg='#f0f0f0')
        self.spk_note.grid(row=2, column=0, columnspan=2, sticky='w')

        # ── Souris ────────────────────────────────────────────────────
        section_header('VISIBILITÉ DE LA SOURIS')
        f = tk.Frame(root, bg='#f0f0f0')
        f.pack(fill='x', padx=PAD * 2, pady=(0, 4))
        self.mouse_var = tk.StringVar(value='normal')
        options = [
            ('normal',  'Curseur normal'),
            ('hidden',  'Curseur caché'),
            ('clicks',  'Visualisation des clics  (G | molette | D)  — rouge au clic'),
        ]
        for val, label in options:
            tk.Radiobutton(f, text=label, variable=self.mouse_var,
                           value=val, bg='#f0f0f0', anchor='w').pack(
                fill='x', pady=2)

        # ── Clavier ───────────────────────────────────────────────────
        section_header('AFFICHAGE DU CLAVIER')
        f = tk.Frame(root, bg='#f0f0f0')
        f.pack(fill='x', padx=PAD * 2, pady=(0, 4))
        self.show_keys_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            f, text='Afficher les frappes en sous-titre  (10 s, effacé à chaque changement de fenêtre)',
            variable=self.show_keys_var, bg='#f0f0f0', wraplength=400,
            justify='left',
        ).pack(anchor='w')

        # ── Délai ─────────────────────────────────────────────────────
        section_header('DÉLAI AVANT DÉMARRAGE')
        f = tk.Frame(root, bg='#f0f0f0')
        f.pack(fill='x', padx=PAD * 2, pady=(0, 8))
        tk.Label(f, text='Décompte (secondes) :', bg='#f0f0f0').grid(
            row=0, column=0, sticky='w')
        self.countdown_var = tk.IntVar(value=5)
        tk.Spinbox(f, from_=0, to=60, textvariable=self.countdown_var,
                   width=5, font=('Arial', 11)).grid(
            row=0, column=1, padx=(10, 0), sticky='w')

        # ── Bouton démarrer ───────────────────────────────────────────
        ttk.Separator(root).pack(fill='x', padx=PAD, pady=(4, 0))
        tk.Button(
            root, text='  ▶  Commencer l\'enregistrement  ',
            font=('Arial', 11, 'bold'),
            bg='#0078d4', fg='white',
            activebackground='#005fa3', activeforeground='white',
            relief='flat', padx=20, pady=10,
            command=self._on_start,
        ).pack(pady=PAD)

    # ------------------------------------------------------------------
    # Flux de l'enregistrement
    # ------------------------------------------------------------------

    def _on_start(self):
        mon_idx = self.monitor_combo.current()
        if mon_idx < 0 or mon_idx >= len(self.monitors):
            messagebox.showerror('Erreur', 'Veuillez sélectionner un écran.')
            return

        mic_idx = self.mic_combo.current()
        mic_device = self.input_devices[mic_idx][1] if mic_idx > 0 else None

        spk_idx = self.spk_combo.current()
        spk_device = self.output_devices[spk_idx][1] if spk_idx > 0 else None

        config = {
            'monitor_info': self.monitors[mon_idx],
            'mic_idx': mic_device,
            'spk_idx': spk_device,
            'mouse_mode': self.mouse_var.get(),
            'show_keys': self.show_keys_var.get(),
        }

        self.root.withdraw()

        n = self.countdown_var.get()
        if n > 0:
            overlay = CountdownOverlay(
                self.root, config['monitor_info'],
                on_done=lambda: self._begin_recording(config),
            )
            overlay.start(n)
        else:
            self._begin_recording(config)

    def _begin_recording(self, config):
        self._recorder = ScreenRecorder(config)
        self._recorder.start_async()
        self._rec_widget = RecordingWidget(self.root, on_stop=self._on_stop)

    def _on_stop(self):
        if self._recorder:
            self._recorder.stop()
        if self._rec_widget:
            self._rec_widget.close()
            self._rec_widget = None
        # Attendre que le thread de capture se termine
        self.root.after(200, self._poll_until_done)

    def _poll_until_done(self):
        if self._recorder and self._recorder.is_running():
            self.root.after(200, self._poll_until_done)
        else:
            if self._recorder:
                self._recorder.wait()
            self._show_save_dialog()

    def _show_save_dialog(self):
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        path = filedialog.asksaveasfilename(
            parent=self.root,
            title="Sauvegarder l'enregistrement",
            initialfile=f'enregistrement_{ts}.mp4',
            defaultextension='.mp4',
            filetypes=[('Vidéo MP4', '*.mp4'), ('Tous les fichiers', '*.*')],
        )

        if not path:
            # Annulé : nettoyer quand même
            if self._recorder:
                self._recorder._cleanup()
            self._recorder = None
            self.root.deiconify()
            return

        # Fenêtre de progression
        prog = tk.Toplevel(self.root)
        prog.title('Sauvegarde...')
        prog.geometry('340x90')
        prog.resizable(False, False)
        prog.attributes('-topmost', True)
        prog.grab_set()
        tk.Label(prog, text='Encodage en cours, veuillez patienter…',
                 font=('Arial', 10)).pack(expand=True)
        pb = ttk.Progressbar(prog, mode='indeterminate')
        pb.pack(fill='x', padx=20, pady=(0, 12))
        pb.start(12)

        recorder = self._recorder

        def do_save():
            ok, msg = recorder.save(path)
            self.root.after(0, lambda: _finish(ok, msg))

        def _finish(ok, msg):
            pb.stop()
            prog.destroy()
            if ok:
                info = f'Enregistrement sauvegardé :\n{path}'
                if msg:
                    info += f'\n\nNote : {msg}'
                messagebox.showinfo('Sauvegardé', info, parent=self.root)
            else:
                messagebox.showerror('Erreur',
                                     f'Échec de la sauvegarde :\n{msg}',
                                     parent=self.root)
            self._recorder = None
            self.root.deiconify()

        threading.Thread(target=do_save, daemon=True).start()

    def _on_close(self):
        if self._recorder:
            if messagebox.askyesno(
                'Enregistrement en cours',
                'Un enregistrement est actif. Voulez-vous l\'arrêter et quitter ?',
            ):
                self._recorder.stop()
                self.root.destroy()
        else:
            self.root.destroy()

    def run(self):
        self.root.mainloop()


# =============================================================================
# Point d'entrée
# =============================================================================

if __name__ == '__main__':
    app = SettingsApp()
    app.run()
