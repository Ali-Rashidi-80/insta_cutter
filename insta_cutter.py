import sys
import os
import subprocess
import json
import datetime
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                               QHBoxLayout, QPushButton, QLabel, QFileDialog, 
                               QProgressBar, QSpinBox, QComboBox, QTextEdit, 
                               QGroupBox, QTabWidget, QMessageBox, QDoubleSpinBox, QFrame)
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QCloseEvent, QFont

# -------------------------------------------------------------------------
# FFMPEG WORKER THREAD (Robust & Fixed)
# -------------------------------------------------------------------------
class VideoWorker(QThread):
    progress_signal = Signal(int)
    log_signal = Signal(str)
    finished_signal = Signal(str)
    error_signal = Signal(str)

    def __init__(self, mode, input_path, output_dir, **kwargs):
        super().__init__()
        self.mode = mode
        self.input_path = input_path
        self.output_dir = output_dir
        self.kwargs = kwargs
        self.is_running = True
        self.current_process = None 

    def run(self):
        try:
            if not self.is_running: return
            
            # Extract Settings
            speed = self.kwargs.get('speed', 1.0)
            
            self.log_signal.emit(f"⏳ Analyzing Video for {self.mode}...")
            width, height, total_duration = self.get_video_info()
            
            self.log_signal.emit(f"🎬 Specs: {width}x{height} | Time: {total_duration:.2f}s | Speed: {speed}x")
            self.log_signal.emit(f"📂 Output: {os.path.basename(self.output_dir)}")

            if self.mode == 'SPLIT':
                self.run_splitter(width, height, total_duration)
            elif self.mode == 'TRIM':
                self.run_trimmer(width, height, total_duration)

        except Exception as e:
            if self.is_running: 
                self.error_signal.emit(str(e))

    def get_video_info(self):
        cmd = [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height,duration",
            "-of", "json", self.input_path
        ]
        startupinfo = self.get_startup_info()
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, startupinfo=startupinfo)
        info = json.loads(result.stdout)
        width = int(info['streams'][0]['width'])
        height = int(info['streams'][0]['height'])
        try:
            duration = float(info['streams'][0]['duration'])
        except KeyError:
            # Fallback for container duration
            cmd_fmt = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", self.input_path]
            res_fmt = subprocess.run(cmd_fmt, stdout=subprocess.PIPE, text=True, startupinfo=startupinfo)
            duration = float(json.loads(res_fmt.stdout)['format']['duration'])
        return width, height, duration

    def get_startup_info(self):
        # Hide console window on Windows
        startupinfo = None
        if os.name == 'nt':
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        return startupinfo

    def get_audio_filter(self, speed):
        if speed == 1.0:
            return None
        temps = []
        s = speed
        # atempo limitation: between 0.5 and 2.0
        while s > 2.0:
            temps.append("atempo=2.0")
            s /= 2.0
        while s < 0.5:
            temps.append("atempo=0.5")
            s /= 0.5
        temps.append(f"atempo={s}")
        return ",".join(temps)

    def get_filter_complex(self, crop_type, speed):
        # 1. Geometry Filter (Dimensions)
        # CRITICAL FIX: Ensure even dimensions using trunc(w/2)*2 to prevent encoding errors
        geo_filter = ""
        if crop_type == "Smart Zoom (Fill Screen)":
            # Crop smallest dimension, ensure even numbers
            geo_filter = r"crop='trunc(min(iw,ih)/2)*2':'trunc(min(iw,ih)/2)*2':(iw-ow)/2:(ih-oh)/2,setsar=1"
        
        elif crop_type == "Blur Background (No Cut)":
            # Complex blur background
            geo_filter = (f"split[main][bg];"
                          f"[bg]scale=iw:iw,boxblur=20:10,setsar=1[bg_blur];"
                          f"[main]scale='trunc(iw*min(1,iw/ih)/2)*2':'-2'[ov];"
                          f"[bg_blur][ov]overlay=(W-w)/2:(H-h)/2")
        
        elif crop_type == "Stretch to Fit":
            geo_filter = r"scale=max(iw\,ih):max(iw\,ih),setsar=1"
        
        else: # Pad
            geo_filter = r"pad=max(iw\,ih):max(iw\,ih):(ow-iw)/2:(oh-ih)/2:black"

        # 2. Speed Filter
        video_speed_filter = f"setpts=PTS/{speed}"
        
        return f"{geo_filter},{video_speed_filter}"

    def run_splitter(self, width, height, total_duration):
        crop_mode = self.kwargs.get('crop_mode')
        overlap_sec = self.kwargs.get('overlap_sec')
        clip_duration = self.kwargs.get('clip_duration')
        speed = self.kwargs.get('speed', 1.0)

        vf_string = self.get_filter_complex(crop_mode, speed)
        af_string = self.get_audio_filter(speed)

        # Calculate source consumption
        effective_clip_duration = clip_duration * speed
        effective_overlap = overlap_sec * speed

        segments = []
        start_time = 0.0
        part_num = 1
        
        while start_time < total_duration:
            if not self.is_running: break
            
            remaining = total_duration - start_time
            current_chunk_source = min(effective_clip_duration, remaining)
            
            # Skip if result is less than 2 seconds (unless it's the only part)
            if (current_chunk_source / speed) < 2 and part_num > 1: break

            output_filename = os.path.join(self.output_dir, f"slide_{part_num:02d}.mp4")
            
            segments.append({
                "start": start_time,
                "duration": current_chunk_source, 
                "output": output_filename
            })
            
            start_time += (effective_clip_duration - effective_overlap)
            part_num += 1

        total_segments = len(segments)
        self.log_signal.emit(f"✂️ Action: Split into {total_segments} parts.")

        for i, seg in enumerate(segments):
            if not self.is_running: break
            
            self.log_signal.emit(f"⚙️ Rendering Part {i+1}/{total_segments}...")
            
            cmd = [
                "ffmpeg", "-y",
                "-ss", f"{seg['start']:.3f}",
                "-t", f"{seg['duration']:.3f}",
                "-i", self.input_path,
                "-filter_complex", vf_string,
                "-c:v", "libx264", "-crf", "18", "-preset", "slow",
                "-c:a", "aac", "-b:a", "192k",
                "-max_muxing_queue_size", "1024" # Prevents buffer errors
            ]
            
            if af_string:
                cmd.extend(["-af", af_string])
            else:
                cmd.extend(["-map", "0:a?"]) # Copy audio if exist
            
            cmd.append(seg['output'])
            self.execute_ffmpeg(cmd)
            self.progress_signal.emit(int(((i + 1) / total_segments) * 100))

        if self.is_running:
            self.finished_signal.emit(f"✅ Done! Files in:\n{self.output_dir}")

    def run_trimmer(self, width, height, total_duration):
        # FIX: Also apply Crop/Aspect ratio logic to Trim mode for consistency
        crop_mode = self.kwargs.get('crop_mode')
        cut_start = self.kwargs.get('trim_start', 0)
        cut_end = self.kwargs.get('trim_end', 0)
        speed = self.kwargs.get('speed', 1.0)
        
        # Calculate Input Duration needed
        source_duration_to_keep = total_duration - cut_start - cut_end
        
        if source_duration_to_keep <= 0.5:
            raise Exception(f"Invalid Duration! Video is {total_duration}s, you cut {cut_start + cut_end}s.")

        output_filename = os.path.join(self.output_dir, f"trimmed_final.mp4")

        vf_string = self.get_filter_complex(crop_mode, speed)
        af_string = self.get_audio_filter(speed)

        self.log_signal.emit(f"✂️ Action: Trim (Start: {cut_start}s, Keep: {source_duration_to_keep}s)")

        # Using -ss BEFORE -i is fast and accurate enough for re-encoding
        cmd = [
            "ffmpeg", "-y",
            "-ss", str(cut_start),
            "-t", str(source_duration_to_keep),
            "-i", self.input_path,
            "-filter_complex", vf_string,
            "-c:v", "libx264", "-crf", "18", "-preset", "slow",
            "-c:a", "aac", "-b:a", "192k",
            "-max_muxing_queue_size", "1024"
        ]
        
        if af_string:
            cmd.extend(["-af", af_string])
        else:
             cmd.extend(["-map", "0:a?"])
            
        cmd.append(output_filename)
        
        self.execute_ffmpeg(cmd)
        if self.is_running:
            self.finished_signal.emit(f"✅ Trimmed file saved:\n{output_filename}")

    def execute_ffmpeg(self, cmd):
        if not self.is_running: return
        
        startupinfo = self.get_startup_info()
        self.current_process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, startupinfo=startupinfo, encoding='utf-8'
        )
        
        stdout, stderr = self.current_process.communicate()
        
        if not self.is_running: return

        if self.current_process.returncode != 0:
            raise Exception(f"FFmpeg Error:\n{stderr}")

    def stop(self):
        self.is_running = False
        if self.current_process:
            try:
                self.log_signal.emit("🛑 Stopping process...")
                self.current_process.kill()
            except:
                pass
        self.quit()
        self.wait()

# -------------------------------------------------------------------------
# GUI MAIN WINDOW (UX Optimized)
# -------------------------------------------------------------------------
class InstaCutterPro(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("InstaCutter Pro - Video Automation")
        self.resize(600, 750)
        self.setAcceptDrops(True)
        self.apply_styles()

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setSpacing(10)
        main_layout.setContentsMargins(15, 15, 15, 15)

        # --- 1. Header ---
        header = QLabel("Video Master Tools")
        header.setObjectName("HeaderLabel")
        header.setAlignment(Qt.AlignCenter)
        main_layout.addWidget(header)

        # --- 2. Drop Zone ---
        self.drop_area = QLabel("\n📂 Drag Video Here\n(Click to Browse)\n")
        self.drop_area.setObjectName("DropArea")
        self.drop_area.setAlignment(Qt.AlignCenter)
        self.drop_area.setCursor(Qt.PointingHandCursor)
        self.drop_area.mousePressEvent = self.browse_file
        main_layout.addWidget(self.drop_area)
        
        self.file_path_label = QLabel("No file selected")
        self.file_path_label.setAlignment(Qt.AlignCenter)
        self.file_path_label.setStyleSheet("color: #7f849c; font-size: 11px;")
        main_layout.addWidget(self.file_path_label)

        # --- 3. Tabs (Operations) ---
        self.tabs = QTabWidget()
        self.tabs.setObjectName("MainTabs")
        
        # TAB 1: Splitter
        self.tab_splitter = QWidget()
        self.init_splitter_tab()
        self.tabs.addTab(self.tab_splitter, "✂️ IG Splitter")

        # TAB 2: Trimmer
        self.tab_trimmer = QWidget()
        self.init_trimmer_tab()
        self.tabs.addTab(self.tab_trimmer, "🔪 Magic Trimmer")

        main_layout.addWidget(self.tabs)

        # --- 4. Global Status Area (Bottom) ---
        status_frame = QFrame()
        status_frame.setObjectName("StatusFrame")
        status_layout = QVBoxLayout(status_frame)
        
        # Progress Bar
        self.progress_bar = QProgressBar()
        self.progress_bar.setAlignment(Qt.AlignCenter)
        self.progress_bar.setValue(0)
        status_layout.addWidget(self.progress_bar)

        # Stop Button & Console
        hbox_status = QHBoxLayout()
        
        self.btn_stop = QPushButton("🛑 STOP OPERATION")
        self.btn_stop.setObjectName("StopBtn")
        self.btn_stop.setFixedWidth(140)
        self.btn_stop.clicked.connect(self.stop_processing)
        self.btn_stop.setEnabled(False)
        hbox_status.addWidget(self.btn_stop)
        
        status_layout.addLayout(hbox_status)
        
        # Console Log
        self.console = QTextEdit()
        self.console.setReadOnly(True)
        self.console.setObjectName("Console")
        self.console.setPlaceholderText("Ready...")
        self.console.setMaximumHeight(100)
        status_layout.addWidget(self.console)

        main_layout.addWidget(status_frame)

        self.input_file = None
        self.worker = None

    # --- UI Generators ---
    def create_speed_control(self):
        """Helper to create speed control row"""
        row = QHBoxLayout()
        row.addWidget(QLabel("Video Speed:"))
        spin = QDoubleSpinBox()
        spin.setRange(0.5, 5.0)
        spin.setSingleStep(0.1)
        spin.setValue(1.0)
        spin.setFixedWidth(70)
        row.addWidget(spin)
        row.addWidget(QLabel("x"))
        row.addStretch()
        return row, spin

    def create_crop_selector(self):
        row = QHBoxLayout()
        row.addWidget(QLabel("Aspect Ratio:"))
        combo = QComboBox()
        combo.addItems([
            "Smart Zoom (Fill Screen)",  
            "Blur Background (No Cut)",  
            "Stretch to Fit",            
            "Pad with Black Bars"        
        ])
        row.addWidget(combo)
        return row, combo

    def init_splitter_tab(self):
        layout = QVBoxLayout(self.tab_splitter)
        layout.setSpacing(15)
        layout.setContentsMargins(10, 20, 10, 10)

        # 1. Settings
        row_crop, self.split_crop = self.create_crop_selector()
        layout.addLayout(row_crop)

        row_speed, self.split_speed = self.create_speed_control()
        layout.addLayout(row_speed)

        # Duration & Overlap
        h_sets = QHBoxLayout()
        
        v1 = QVBoxLayout()
        v1.addWidget(QLabel("Overlap (sec):"))
        self.spin_overlap = QSpinBox()
        self.spin_overlap.setRange(0, 5)
        self.spin_overlap.setValue(1)
        v1.addWidget(self.spin_overlap)
        h_sets.addLayout(v1)

        v2 = QVBoxLayout()
        v2.addWidget(QLabel("Clip Time (sec):"))
        self.spin_duration = QSpinBox()
        self.spin_duration.setRange(10, 60)
        self.spin_duration.setValue(59)
        v2.addWidget(self.spin_duration)
        h_sets.addLayout(v2)
        
        layout.addLayout(h_sets)
        
        layout.addStretch()

        # 2. Big Action Button
        self.btn_split = QPushButton("START SPLITTING 🚀")
        self.btn_split.setObjectName("ActionBtn")
        self.btn_split.setFixedHeight(50)
        self.btn_split.clicked.connect(self.start_split)
        self.btn_split.setEnabled(False)
        layout.addWidget(self.btn_split)

    def init_trimmer_tab(self):
        layout = QVBoxLayout(self.tab_trimmer)
        layout.setSpacing(15)
        layout.setContentsMargins(10, 20, 10, 10)

        # Settings
        row_crop, self.trim_crop = self.create_crop_selector()
        layout.addLayout(row_crop)

        row_speed, self.trim_speed = self.create_speed_control()
        layout.addLayout(row_speed)

        # Cuts
        cuts_grp = QGroupBox("Cut Seconds (Remove from...)")
        h_cuts = QHBoxLayout()
        
        v1 = QVBoxLayout()
        v1.addWidget(QLabel("Start:"))
        self.spin_trim_start = QSpinBox()
        self.spin_trim_start.setRange(0, 3600)
        v1.addWidget(self.spin_trim_start)
        h_cuts.addLayout(v1)

        v2 = QVBoxLayout()
        v2.addWidget(QLabel("End:"))
        self.spin_trim_end = QSpinBox()
        self.spin_trim_end.setRange(0, 3600)
        v2.addWidget(self.spin_trim_end)
        h_cuts.addLayout(v2)
        
        cuts_grp.setLayout(h_cuts)
        layout.addWidget(cuts_grp)
        
        layout.addStretch()

        # Big Action Button
        self.btn_trim = QPushButton("START TRIMMING ✂️")
        self.btn_trim.setObjectName("ActionBtn")
        self.btn_trim.setFixedHeight(50)
        self.btn_trim.clicked.connect(self.start_trim)
        self.btn_trim.setEnabled(False)
        layout.addWidget(self.btn_trim)

    # --- Styles ---
    def apply_styles(self):
        self.setStyleSheet("""
            QMainWindow { background-color: #1e1e2e; }
            QWidget { font-family: 'Segoe UI', sans-serif; font-size: 14px; }
            QLabel { color: #cdd6f4; }
            QLabel#HeaderLabel { font-size: 24px; font-weight: bold; color: #cba6f7; margin-bottom: 10px; }
            
            /* Drop Area */
            QLabel#DropArea { 
                border: 2px dashed #45475a; border-radius: 12px; background-color: #262736; 
                color: #89b4fa; font-size: 16px; 
            }
            QLabel#DropArea:hover { border-color: #89b4fa; background-color: #313244; }

            /* Tabs */
            QTabWidget::pane { border: 1px solid #45475a; border-radius: 6px; background: #1e1e2e; }
            QTabBar::tab {
                background: #313244; color: #a6adc8; padding: 10px 20px; 
                border-top-left-radius: 6px; border-top-right-radius: 6px; margin-right: 2px;
            }
            QTabBar::tab:selected { background: #89b4fa; color: #1e1e2e; font-weight: bold; }

            /* Controls */
            QComboBox, QSpinBox, QDoubleSpinBox { 
                background-color: #313244; border: 1px solid #45475a; 
                color: #ffffff; padding: 8px; border-radius: 6px; 
            }
            QGroupBox { 
                border: 1px solid #45475a; border-radius: 8px; margin-top: 20px; 
                color: #fab387; font-weight: bold; 
            }
            QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px; }

            /* Action Buttons */
            QPushButton#ActionBtn {
                background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #a6e3a1, stop:1 #94e2d5);
                color: #1e1e2e; border-radius: 8px; font-weight: bold; font-size: 16px; border: none;
            }
            QPushButton#ActionBtn:hover { background-color: #f9e2af; }
            QPushButton#ActionBtn:disabled { background-color: #45475a; color: #7f849c; }

            /* Stop Button */
            QPushButton#StopBtn {
                background-color: #f38ba8; color: #1e1e2e; border-radius: 8px; font-weight: bold; border: none;
            }
            QPushButton#StopBtn:hover { background-color: #eba0ac; }
            QPushButton#StopBtn:disabled { background-color: #45475a; color: #7f849c; }

            /* Status Frame */
            QFrame#StatusFrame { background-color: #181825; border-radius: 10px; margin-top: 10px; }
            QTextEdit#Console { background-color: #11111b; border: none; color: #a6adc8; font-size: 12px; }
            QProgressBar { border: none; border-radius: 5px; background: #313244; height: 10px; text-align: center; }
            QProgressBar::chunk { background-color: #89b4fa; border-radius: 5px; }
        """)

    # --- Logic ---
    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls(): event.accept()
        else: event.ignore()

    def dropEvent(self, event: QDropEvent):
        files = [u.toLocalFile() for u in event.mimeData().urls()]
        if files: self.load_file(files[0])

    def browse_file(self, event):
        path, _ = QFileDialog.getOpenFileName(self, "Select Video", "", "Video Files (*.mp4 *.mov *.mkv)")
        if path: self.load_file(path)

    def load_file(self, path):
        self.input_file = path
        self.file_path_label.setText(path)
        self.drop_area.setText(f"✅ Selected: {os.path.basename(path)}")
        self.log(f"Loaded: {os.path.basename(path)}")
        self.btn_split.setEnabled(True)
        self.btn_trim.setEnabled(True)

    def start_split(self):
        self.start_worker(mode='SPLIT', 
                          crop_mode=self.split_crop.currentText(),
                          overlap_sec=self.spin_overlap.value(),
                          clip_duration=self.spin_duration.value(),
                          speed=self.split_speed.value())

    def start_trim(self):
        if self.spin_trim_start.value() == 0 and self.spin_trim_end.value() == 0:
            # Maybe just converting or aspect ratio change? Allow it.
            pass
        self.start_worker(mode='TRIM',
                          crop_mode=self.trim_crop.currentText(), # Added Crop support to Trim
                          trim_start=self.spin_trim_start.value(),
                          trim_end=self.spin_trim_end.value(),
                          speed=self.trim_speed.value())

    def start_worker(self, mode, **kwargs):
        if not self.input_file: return
        
        # SMART FOLDER CREATION
        base_name = os.path.splitext(os.path.basename(self.input_file))[0]
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        folder_name = f"{base_name}_{mode}_{timestamp}"
        output_dir = os.path.join(os.path.dirname(self.input_file), folder_name)
        
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)

        self.lock_ui(True)
        self.log(f"🚀 Started {mode}...")

        self.worker = VideoWorker(mode, self.input_file, output_dir, **kwargs)
        self.worker.log_signal.connect(self.log)
        self.worker.progress_signal.connect(self.progress_bar.setValue)
        self.worker.finished_signal.connect(self.on_finished)
        self.worker.error_signal.connect(self.on_error)
        self.worker.start()

    def stop_processing(self):
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.log("🛑 Stopped by user.")
            self.lock_ui(False)
            self.progress_bar.setValue(0)

    def log(self, msg):
        self.console.append(msg)
        self.console.verticalScrollBar().setValue(self.console.verticalScrollBar().maximum())

    def lock_ui(self, locked):
        self.btn_split.setEnabled(not locked)
        self.btn_trim.setEnabled(not locked)
        self.drop_area.setEnabled(not locked)
        self.btn_stop.setEnabled(locked) 
        if locked: self.progress_bar.setValue(0)

    def on_finished(self, msg):
        self.log(msg)
        self.lock_ui(False)
        self.progress_bar.setValue(100)

    def on_error(self, err):
        self.log(f"❌ ERROR: {err}")
        self.lock_ui(False)
        self.progress_bar.setStyleSheet("QProgressBar::chunk { background-color: #f38ba8; }")
    
    def closeEvent(self, event: QCloseEvent):
        if self.worker and self.worker.isRunning():
            self.worker.stop()
        event.accept()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = InstaCutterPro()
    window.show()
    sys.exit(app.exec())