from PyQt5.QtGui import QTextCursor
import autosub_API
from translation import get_token, read_subtitle_file, translate_text
from PyQt5.QtWidgets import QWidget, QVBoxLayout, QPushButton, QComboBox, QTextEdit, QFileDialog, QLabel, QHBoxLayout, \
    QDialog
from PyQt5.QtCore import QThread, pyqtSignal, Qt
import os
from videotosrt import get_api_keys
from translation import get_translation_keys
from PyQt5.QtWidgets import QApplication, QMessageBox
from PyQt5.QtWidgets import QProgressBar
from PyQt5.QtGui import QIcon
from multiprocessing import freeze_support
from pathlib import Path
from audio_translation import translate_media_to_english_mp3
from subtitle_style_dialog import SubtitleStyleDialog
from subtitle_editor_dialog import (
    SubtitleEditorDialog,
    parse_srt_text,
    SrtParseError,
)
from playback_state import get_playback_state
from mpv_controller import MpvController
from subtitle_export import SubtitleConfig


class AudioTranslationThread(QThread):
    finished_signal = pyqtSignal(str)
    error_signal = pyqtSignal(str)
    progress_signal = pyqtSignal(int, str)

    def __init__(self, media_path, output_path, speech_keys, translation_keys, source_language, target_language):
        super().__init__()
        self.media_path = media_path
        self.output_path = output_path
        self.speech_keys = speech_keys
        self.translation_keys = translation_keys
        self.source_language = source_language
        self.target_language = target_language

    def run(self):
        try:
            output = translate_media_to_english_mp3(
                self.media_path,
                self.output_path,
                self.speech_keys,
                self.translation_keys,
                progress=lambda value, message: self.progress_signal.emit(value, message),
                source_language=self.source_language,
                target_language=self.target_language,
            )
            self.finished_signal.emit(output)
        except Exception as exc:
            self.error_signal.emit(str(exc))


class GenerateSubtitlesThread(QThread):
    signal = pyqtSignal(str)
    progress_signal = pyqtSignal(int)  # 新增进度信号

    def __init__(self, video_path, dev_pid):
        super().__init__()
        self.video_path = video_path
        self.dev_pid = dev_pid

    def run(self):
        # 修改autosub_API.start调用，传递进度回调函数
        result = autosub_API.start(self.video_path, self.dev_pid, self.update_progress)
        self.signal.emit(result)

    def update_progress(self, value):
        self.progress_signal.emit(value)

class TranslateSubtitleThread(QThread):
    # 翻译结果通过 Qt 的 object 信号直接传回内存中的字幕记录；线程不负责
    # 写文件，只有用户在校对面板点击“保存”后才会落盘。
    signal = pyqtSignal(object)
    error_signal = pyqtSignal(str)
    progress_signal = pyqtSignal(int)  # 用于更新进度条的信号

    def __init__(self, subtitle_path, to_lang, include_original=True):
        super().__init__()
        self.subtitle_path = subtitle_path
        self.to_lang = to_lang
        self.include_original = include_original

    def run(self):
        try:
            token = get_token()
            if not token:
                raise RuntimeError("获取百度翻译令牌失败，请检查网络和翻译密钥。")
            subtitle_content = read_subtitle_file(self.subtitle_path)

            # 先解析源字幕并保留原文。翻译函数返回的 SRT 在双语模式下
            # 会把“译文”和“原文”拼成一个字幕块；如果这里只把翻译后的
            # ``text`` 传给编辑器，校对时就无法知道哪一部分是原文。因此
            # 这里同时保留源字幕记录，后面按序号/位置与翻译结果合并成
            # ``original_text`` + ``translated_text`` 的四字段结构。
            source_entries = parse_srt_text(subtitle_content)
            if not source_entries:
                raise SrtParseError("源字幕为空，无法开始翻译。")

            # 调用翻译函数时传递更新进度的回调。translate_text 返回 SRT 文本，
            # 这里立即解析成结构化记录并通过信号传给 GUI；本线程绝不写入
            # *_translated.srt，避免用户尚未校对的机器翻译污染磁盘文件。
            translated_content = translate_text(
                subtitle_content,
                token,
                self.to_lang,
                self.include_original,
                progress_callback=self.update_progress,
            )
            translated_entries = parse_srt_text(translated_content)
            if not translated_entries:
                raise SrtParseError("翻译结果为空，无法打开校对面板。")
            if len(translated_entries) != len(source_entries):
                raise SrtParseError(
                    "翻译结果与源字幕条数不一致："
                    f"源字幕 {len(source_entries)} 条，翻译结果 {len(translated_entries)} 条。"
                )

            # ``translate_text`` 应当逐条保留时间轴，但为了防止网络返回
            # 内容不完整或第三方翻译器调整了序号，这里按序号优先、位置
            # 其次进行配对。缺少任何一条时直接报错，避免把错误的译文
            # 错配到另一条字幕上。
            translated_by_index = {
                str(entry.get("index")): entry for entry in translated_entries
            }
            merged_entries = []
            for position, source_entry in enumerate(source_entries):
                translated_entry = translated_by_index.get(str(source_entry.get("index")))
                if translated_entry is None and position < len(translated_entries):
                    translated_entry = translated_entries[position]
                if translated_entry is None:
                    raise SrtParseError(
                        f"第 {position + 1} 条字幕没有对应的翻译结果。"
                    )

                translated_text = str(
                    translated_entry.get("text", "")
                )
                # 对校对表格而言，源时间轴是唯一可信的时间轴；翻译 API
                # 只负责替换内容，避免其返回的序号/时间轴被误改后导致
                # 保存出的 SRT 与原片不同步。
                merged_entries.append(
                    {
                        "index": source_entry.get("index", position + 1),
                        "time": source_entry.get("time", ""),
                        "original_text": str(source_entry.get("text", "")),
                        "translated_text": translated_text,
                    }
                )

            source = Path(self.subtitle_path)
            suffix = "zh&en" if self.include_original else self.to_lang
            payload = {
                "entries": merged_entries,
                "source_path": self.subtitle_path,
                "default_output_path": str(
                    source.with_name(f"{source.stem}_translated.srt")
                ),
                "suffix": suffix,
            }
            self.signal.emit(payload)
        except Exception as exc:
            # 线程异常必须通过信号返回，避免后台线程异常导致主程序退出。
            self.error_signal.emit(str(exc))

    def update_progress(self, value):
        self.progress_signal.emit(value)
class MyApp(QWidget):
    # mpv IPC reader 在线程中收到 Lua 菜单事件后，只发 Qt 信号；真正的
    # 对话框和 QThread 创建始终在 GUI 线程执行。
    mpv_export_requested = pyqtSignal()

    def __init__(self):
        super().__init__()
        # 播放器与导出面板共享同一个线程安全状态中枢。mpv OSD 菜单
        # 改动的字号、位置和延迟会实时写入这里，导出时读取一次快照。
        self.playback_state = get_playback_state()
        # 主窗口持有唯一的 GUI 字幕样式对象。手动导出面板和 mpv 菜单
        # 触发的导出都引用它，避免自动导出重新创建默认样式。
        self.subtitle_config = SubtitleConfig()
        self.mpv_controller = None
        self.current_video_path = None
        self.current_subtitle_path = None
        self.mpv_export_requested.connect(self._export_from_mpv)
        self.initUI()
    def showSuccessMessage(self):
        msg = QMessageBox()
        msg.setIcon(QMessageBox.Information)
        msg.setText("字幕文件生成完毕！")
        msg.setWindowTitle("成功")
        msg.setStandardButtons(QMessageBox.Ok)
        retval = msg.exec_()

    def showHelpMessage(self):
        helpDialog = QDialog(self)
        helpDialog.setWindowTitle("帮助")
        helpDialog.setFixedSize(400, 200)

        helpText = QTextEdit(helpDialog)
        helpText.setReadOnly(True)  # 设置为只读模式
        helpText.setText("第一次使用请去https://console.bce.baidu.com/\n"
                         "网站里面登录账号，创建语音技术和机器翻译的应用，\n"
                         "获取对应的apikey(API_KEY和SECRET_KEY)\n"
                         "再来使用本软件，生成字幕会保存到与视频同目录下。")  # 帮助信息内容
        helpText.setLineWrapMode(QTextEdit.NoWrap)  # 设置不自动换行
        helpText.selectAll()
        helpText.copy()  # 复制文本到剪贴板
        helpText.moveCursor(QTextCursor.Start)  # 移动光标到文本开始位置
        helpText.setFocusPolicy(Qt.NoFocus)  # 文本框不接受焦点

        layout = QVBoxLayout(helpDialog)
        layout.addWidget(helpText)

        closeButton = QPushButton("关闭", helpDialog)
        closeButton.clicked.connect(helpDialog.close)
        layout.addWidget(closeButton)

        helpDialog.setLayout(layout)
        helpDialog.exec_()

    def initUI(self):

        helpLabel = QLabel("😺点击下方按钮使用对应功能，请先看帮助→", self)
        helpButton = QPushButton('帮助', self)
        helpButton.clicked.connect(self.showHelpMessage)
        self.progressBar = QProgressBar(self)
        self.progressBar.setGeometry(200, 80, 250, 20)

        # 创建下拉框并添加选项
        self.langComboBox = QComboBox(self)
        self.langComboBox.addItem("识别普通话", 1537)
        self.langComboBox.addItem("识别英语", 1737)
        self.langComboBox.setFixedWidth(200)

        self.comboBox = QComboBox(self)
        self.comboBox.addItem("中文转换成中英双语")
        self.comboBox.addItem("英文转换成中英双语")
        # 添加两个新的选项
        self.comboBox.addItem("中文转换成英文")
        self.comboBox.addItem("英文转换成中文")
        self.comboBox.setFixedWidth(200)

        btn1 = QPushButton('播放本地视频', self)
        btn1.clicked.connect(self.openVideo)
        self.progressBar.setStyleSheet("""
            QProgressBar {
                border: 2px solid grey;
                border-radius: 5px;
                text-align: center; 
            }
            QProgressBar::chunk {
                background-color: #05B8CC;
                width: 20px; 
            }
        """)
        btn2 = QPushButton('视频生成字幕', self)
        btn2.clicked.connect(self.generateSubtitles)

        btn3 = QPushButton('字幕文件翻译', self)
        btn3.clicked.connect(self.translateSubtitle)

        self.audioDirectionComboBox = QComboBox(self)
        self.audioDirectionComboBox.addItem("中文转换为英文")
        self.audioDirectionComboBox.addItem("英文转换成中文")
        self.audioDirectionComboBox.setFixedWidth(200)

        btnAudio = QPushButton('音频/视频翻译为 MP3', self)
        btnAudio.clicked.connect(self.translateAudioToMp3)
        self.audioButton = btnAudio

        btnHardSubtitle = QPushButton('字幕样式/硬字幕导出', self)
        btnHardSubtitle.clicked.connect(self.openSubtitleExport)

        btn4 = QPushButton('退出', self)
        btn4.clicked.connect(QApplication.instance().quit)
        self.textbox = QTextEdit(self)  # 创建一个文本框

        # 创建水平布局并添加提示标签和帮助按钮
        hboxTop = QHBoxLayout()
        hboxTop.addWidget(helpLabel)
        hboxTop.addWidget(helpButton)

        # 创建水平布局用于视频生成字幕和语言选择
        hboxGenerateSubtitles = QHBoxLayout()
        hboxGenerateSubtitles.addWidget(self.langComboBox)
        hboxGenerateSubtitles.addWidget(btn2)
        # self.langComboBox.setFixedWidth(btn2.sizeHint().width())  # 设置下拉框宽度与按钮相同

        # 创建水平布局用于字幕文件翻译和语言选择
        hboxTranslateSubtitle = QHBoxLayout()
        hboxTranslateSubtitle.addWidget(self.comboBox)
        hboxTranslateSubtitle.addWidget(btn3)

        hboxAudioTranslation = QHBoxLayout()
        hboxAudioTranslation.addWidget(self.audioDirectionComboBox)
        hboxAudioTranslation.addWidget(btnAudio)

        vbox = QVBoxLayout()
        vbox.addLayout(hboxTop)  # 添加顶部的水平布局到垂直布局
        vbox.addWidget(btn1)
        vbox.addLayout(hboxGenerateSubtitles)  # 添加视频生成字幕和语言选择的水平布局
        vbox.addLayout(hboxTranslateSubtitle)  # 添加字幕文件翻译和语言选择的水平布局
        vbox.addLayout(hboxAudioTranslation)
        vbox.addWidget(btnHardSubtitle)
        vbox.addWidget(btn4)
        vbox.addWidget(self.progressBar)
        vbox.addWidget(self.textbox)  # 把文本框添加到布局中

        self.setLayout(vbox)

        self.setWindowIcon(QIcon('./icon/主界面图标.png'))
        self.setWindowTitle('视频字幕生成工具')
        self.setGeometry(400, 400, 400, 400)
        self.show()
    def openVideo(self):
        fname = QFileDialog.getOpenFileName(self, 'Open file', './')
        if fname[0]:
            mpv_path = Path(__file__).resolve().parent / "mpv" / ("mpv.exe" if os.name == "nt" else "mpv")
            try:
                # Popen + JSON IPC 让 GUI 保持响应，并持续监听 mpv 的
                # property-change 事件。关闭播放器时控制器会先查询最后
                # 一次属性，再持久化到 portable_config/persistent_config.json。
                if self.mpv_controller is None:
                    self.mpv_controller = MpvController(
                        mpv_path,
                        config_dir=Path(__file__).resolve().parent / "mpv" / "portable_config",
                        state=self.playback_state,
                    )
                    self.mpv_controller.add_event_listener(self._on_mpv_event)
                self.current_video_path = fname[0]
                self.mpv_controller.start(fname[0])
                self.textbox.setText("mpv 播放器已启动，右键菜单调整的字幕参数会同步到导出面板。")
            except Exception as exc:
                self.mpv_controller = None
                QMessageBox.critical(self, "播放器启动失败", str(exc))

    def translateSubtitle(self):

        ak, sk = get_translation_keys()
        if ak is None or sk is None:
            QMessageBox.warning(self, "错误", "AK或SK无效，请检查translationkey.txt文件。")
            return
        choice = self.comboBox.currentText()
        if choice == "中文转换成中英双语":
            to_lang = 'en'
            include_original = True
        elif choice == "英文转换成中英双语":
            to_lang = 'zh'
            include_original = True
        elif choice == "中文转换成英文":
            to_lang = 'en'
            include_original = False  # 不包含原文
        elif choice == "英文转换成中文":
            to_lang = 'zh'
            include_original = False

        fname = QFileDialog.getOpenFileName(self, 'Select Subtitle', './')
        if fname[0]:
            # 仅记录源文件供线程读取和生成建议文件名。翻译结果暂存内存，
            # 在用户完成校对并点击保存前，不改变当前可用于硬字幕导出的路径。
            self.textbox.setText("字幕文件正在翻译，请耐心等待...")
            # 在这里创建线程时传递include_original参数
            self.thread = TranslateSubtitleThread(fname[0], to_lang, include_original)
            self.thread.signal.connect(self.onTranslationFinished)
            self.thread.error_signal.connect(self.onTranslationFailed)
            self.thread.progress_signal.connect(self.progressBar.setValue)  # 更新进度条
            self.thread.start()

    def generateSubtitles(self):
        API_KEY, SECRET_KEY = get_api_keys()
        if not API_KEY or not SECRET_KEY:
            QMessageBox.warning(self, "错误", "API_KEY或SECRET_KEY无效，请检查。")
            return
        fname = QFileDialog.getOpenFileName(self, 'Select Video', './')
        if fname[0]:
            print("开始生成字幕...")
            self.textbox.setText("字幕文件正在生成，请耐心等待...")
            dev_pid = self.langComboBox.currentData()  # 从下拉框获取dev_pid值
            self.progressBar.setValue(0)  # 开始生成字幕前，进度条设置为0
            self.thread = GenerateSubtitlesThread(fname[0], dev_pid)
            print("线程创建成功")
            self.thread.signal.connect(self.onFinished)
            self.thread.progress_signal.connect(self.progressBar.setValue)
            self.thread.start()
            print("线程已启动")

    def translateAudioToMp3(self):
        choice = self.audioDirectionComboBox.currentText()
        if choice == "英文转换成中文":
            source_language, target_language = "en", "zh"
            source_label, output_suffix = "英文", "zh"
        elif choice == "中文转换为英文":
            source_language, target_language = "zh", "en"
            source_label, output_suffix = "中文", "en"
        else:
            QMessageBox.warning(self, "错误", "请选择音频翻译方向。")
            return

        speech_api_key, speech_secret_key = get_api_keys()
        if not speech_api_key or not speech_secret_key:
            QMessageBox.warning(self, "错误", "语音识别/合成密钥无效，请检查 apikey.txt。")
            return

        translation_ak, translation_sk = get_translation_keys()
        if not translation_ak or not translation_sk:
            QMessageBox.warning(self, "错误", "机器翻译密钥无效，请检查 translationkey.txt。")
            return

        media_path, _ = QFileDialog.getOpenFileName(
            self,
            f"选择{source_label}音频或视频",
            "./",
            "媒体文件 (*.mp3 *.wav *.m4a *.aac *.flac *.ogg *.mp4 *.mkv *.mov *.avi *.webm);;所有文件 (*)",
        )
        if not media_path:
            return

        source = Path(media_path)
        output_path = str(source.with_name(source.stem + f"-{output_suffix}.mp3"))
        if Path(output_path).exists():
            answer = QMessageBox.question(
                self,
                "覆盖文件？",
                f"目标文件已存在，是否覆盖？\n{output_path}",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return

        self.progressBar.setValue(0)
        self.textbox.setText(f"正在准备{source_label}音频翻译…")
        self.audioButton.setEnabled(False)
        self.audioThread = AudioTranslationThread(
            media_path,
            output_path,
            (speech_api_key, speech_secret_key),
            (translation_ak, translation_sk),
            source_language=source_language,
            target_language=target_language,
        )
        self.audioThread.progress_signal.connect(self.onAudioProgress)
        self.audioThread.finished_signal.connect(self.onAudioFinished)
        self.audioThread.error_signal.connect(self.onAudioFailed)
        self.audioThread.start()

    def onAudioProgress(self, value, message):
        self.progressBar.setValue(value)
        self.textbox.setText(message)

    def onAudioFinished(self, output_path):
        self.audioButton.setEnabled(True)
        self.textbox.setText("翻译 MP3 已保存到：\n" + output_path)
        QMessageBox.information(self, "完成", "翻译 MP3 已生成：\n" + output_path)

    def onAudioFailed(self, error):
        self.audioButton.setEnabled(True)
        self.textbox.setText("MP3 生成失败：\n" + error)
        QMessageBox.critical(self, "生成失败", error)

    def onTranslationFinished(self, payload):
        """接收内存中的机翻结果并立即打开校对面板。

        ``TranslateSubtitleThread`` 只负责调用 API 和解析 SRT，不写本地文件。
        只有编辑器返回 ``saved=True`` 时，才把最终路径交给后续硬字幕流程。
        用户取消/关闭窗口时，内存结果随对话框释放，不会产生半成品文件。
        """

        if not isinstance(payload, dict):
            self.onTranslationFailed("翻译线程返回了无效的数据。")
            return
        entries = payload.get("entries")
        source_path = str(payload.get("source_path") or "")
        default_output_path = str(payload.get("default_output_path") or "")
        if not entries:
            self.onTranslationFailed("翻译结果为空，无法打开校对面板。")
            return

        self.progressBar.setValue(100)
        self.textbox.setText("机器翻译完成，请在校对面板中检查并保存。")
        try:
            dialog = SubtitleEditorDialog(
                subtitle_path=source_path or None,
                parent=self,
                entries=entries,
                default_output_path=default_output_path or None,
            )
        except Exception as exc:
            self.onTranslationFailed(f"打开字幕校对面板失败：{exc}")
            return
        if not dialog.is_loaded:
            return

        result = dialog.exec_()
        if result == QDialog.Accepted and getattr(dialog, "saved", False):
            saved_path = str(getattr(dialog, "subtitle_path", "") or "")
            if saved_path:
                self.current_subtitle_path = saved_path
                self.textbox.setText("字幕校对完成，文件已保存到：\n" + saved_path)
                return
        self.textbox.setText("已取消字幕校对，翻译结果未保存。")

    def onTranslationFailed(self, error):
        """在主线程中显示翻译或解析失败原因。"""

        self.progressBar.setValue(0)
        message = str(error or "字幕翻译失败。")
        self.textbox.setText("字幕翻译失败：\n" + message)
        QMessageBox.critical(self, "翻译失败", message)

    def openSubtitleExport(self):
        """打开字幕样式面板，在独立线程中执行硬字幕压制。"""
        dialog = SubtitleStyleDialog(
            self,
            config=self.subtitle_config,
            playback_state=self.playback_state if self._mpv_is_running() else None,
        )
        dialog.export_requested.connect(self._remember_export_paths)
        dialog.exec_()

    def _remember_export_paths(self, video, subtitle, _output, _config):
        """保存最近一次手动导出的字幕路径，供 mpv 一键导出复用。"""

        self.current_video_path = str(video)
        self.current_subtitle_path = str(subtitle)

    def _on_mpv_event(self, message):
        """在 IPC 线程中接收 Lua 的 client-message，再切回 Qt 线程。"""

        if message.get("event") not in ("client-message", "script-message"):
            return
        args = message.get("args") or []
        if args and str(args[0]) == "export-video-now":
            self.mpv_export_requested.emit()

    def _export_from_mpv(self):
        """用当前播放视频及同名 SRT 直接打开导出面板并自动开始压制。"""

        video = self.current_video_path
        if self.mpv_controller is not None and self.mpv_controller.media_path:
            video = self.mpv_controller.media_path
        if not video or not os.path.isfile(video):
            self.textbox.setText("一键导出失败：当前没有可用的视频文件。")
            return

        source = Path(video)
        # 生成字幕通常会保存为同名 .srt；同时兼容本项目翻译输出的常见后缀。
        candidates = [
            Path(self.current_subtitle_path)
            if self.current_subtitle_path
            and self.current_video_path
            and os.path.normcase(os.path.abspath(self.current_video_path))
            == os.path.normcase(os.path.abspath(video))
            else None,
            source.with_name(source.stem + "-zh&en.srt"),
            source.with_name(source.stem + "-zh.srt"),
            source.with_name(source.stem + "-en.srt"),
            source.with_suffix(".srt"),
        ]
        subtitle = next((str(path) for path in candidates if path is not None and path.is_file()), None)
        if subtitle is None:
            message = "一键导出失败：未找到与视频同名的 SRT 字幕文件。"
            self.textbox.setText(message)
            if self.mpv_controller is not None:
                try:
                    self.mpv_controller.command("show-text", message, 5000)
                except (OSError, RuntimeError, ValueError):
                    pass
            return

        # 面板仍会显示当前 IPC 快照和进度，但 auto_start_export 使菜单点击
        # 后无需再切回主界面或重复点击按钮。
        dialog = SubtitleStyleDialog(
            self,
            video_path=str(source),
            subtitle_path=subtitle,
            config=self.subtitle_config,
            playback_state=self.playback_state,
            auto_start_export=True,
        )
        dialog.exec_()

    def _mpv_is_running(self):
        return self.mpv_controller is not None and self.mpv_controller.running

    def closeEvent(self, event):
        # 关闭主窗口前同步最后一帧 mpv 属性，防止用户刚在 OSD 中调整的
        # 参数尚未进入 persistent_config.json 就被进程结束。
        if self.mpv_controller is not None:
            try:
                self.mpv_controller.close(terminate=True, persist=True)
            except Exception:
                pass
            self.mpv_controller = None
        event.accept()

    def onFinished(self, result):
        # 视频生成字幕线程返回已落盘的 SRT 路径。
        if result and str(result).lower().endswith(".srt") and os.path.isfile(str(result)):
            self.current_subtitle_path = str(result)
        self.textbox.setText("文件生成地址：" + result)
        self.showSuccessMessage()

if __name__ == '__main__':
    freeze_support()  # 在程序入口处调用freeze_support
    import sys
    app = QApplication(sys.argv)
    ex = MyApp()
    sys.exit(app.exec_())
