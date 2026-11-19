"""State and navigation contracts of the redesigned shell, with isolated data."""

import os
import time
from pathlib import Path
from unittest.mock import Mock

os.environ['QT_QPA_PLATFORM'] = 'offscreen'

import pytest  # noqa: E402
from PySide6.QtCore import QCoreApplication, QEvent, Qt  # noqa: E402
from PySide6.QtGui import QFontDatabase  # noqa: E402
from PySide6.QtWidgets import QAbstractItemView, QApplication  # noqa: E402

from deal_watch.ui.main import MainWindow  # noqa: E402
from deal_watch.ui.shell import PageId, RecordView  # noqa: E402
from region_fixtures import ready_region  # noqa: E402


@pytest.fixture
def window(tmp_path, monkeypatch):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    app = QApplication.instance() or QApplication([])
    for name in ('segoeui.ttf', 'msyh.ttc'):
        QFontDatabase.addApplicationFont('C:/Windows/Fonts/' + name)
    app.setQuitOnLastWindowClosed(False)
    w = MainWindow(Path(__file__).resolve().parents[1])
    w._initial_geometry_applied = True
    w.timer.stop()
    w.health_timer.stop()
    w.dispatcher.stop()
    deadline = time.monotonic() + 3
    while not w._recognition_ready and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.005)
    assert w._recognition_ready
    w.records_refresh.request = Mock(return_value=False)
    w.recognition_refresh.request = Mock(return_value=False)
    w.show()
    app.processEvents()
    yield w, app
    w.service = None
    w.exit_app()
    deadline = time.monotonic() + 4
    while not w._shutdown_done and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.005)
    assert w._shutdown_done
    w.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


def records(count=200):
    return [dict(id=f'r{i}', observed_at=1700000000-i, region_alias='合成区域',
                 keyword='优惠', text=f'合成消息 {i}', state='sent', attempts=1)
            for i in range(count)]


def test_theme_switch_keeps_draft_page_selection_and_scroll(window):
    w, app = window
    w.keywords_edit.setPlainText('原关键词')
    w.keywords_edit.input.setText('新关键词')
    w.keywords_edit.add_button.click()
    draft = w.keywords_edit.toPlainText()
    assert w.keywords_edit.isModified()
    w.navigate(PageId.RECORDS, RecordView.NOTIFICATIONS)
    w._apply_records(records())
    w.record_table.setCurrentCell(90, 0)
    w.record_table.selectRow(90)
    w.record_table.scrollToItem(w.record_table.item(60, 0), QAbstractItemView.ScrollHint.PositionAtTop)
    position = w.record_table.verticalScrollBar().value()
    widgets = (w.keywords_edit, w.record_table, w.region_table)
    for mode in ('dark', 'light', 'system'):
        w.theme_manager.set_mode(mode)
        app.processEvents()
        assert w.keywords_edit.toPlainText() == draft and w.keywords_edit.isModified()
        assert w.pages.currentIndex() == PageId.RECORDS
        assert w.record_table.item(w.record_table.currentRow(), 0).data(Qt.ItemDataRole.UserRole) == 'r90'
        assert w.record_table.verticalScrollBar().value() == position
        assert (w.keywords_edit, w.record_table, w.region_table) == widgets
    assert not w.config_store.path.exists()
    assert w.theme_manager.wait_saved(2)
    assert w.theme_manager.path.is_file()


def test_record_refresh_preserves_visible_anchor_and_selection_by_id(window):
    w, app = window
    w.navigate(PageId.RECORDS, RecordView.NOTIFICATIONS)
    rows = records()
    w._apply_records(rows)
    app.processEvents()
    w.record_table.setCurrentCell(150, 0)
    w.record_table.selectRow(150)
    w.record_table.scrollToItem(w.record_table.item(50, 0), QAbstractItemView.ScrollHint.PositionAtTop)
    anchor = w.record_table.item(w.record_table.rowAt(0), 0).data(Qt.ItemDataRole.UserRole)
    new = dict(rows[0], id='new', text='合成新消息')
    w._apply_records([new] + rows[:-1])
    assert w.record_table.item(w.record_table.currentRow(), 0).data(Qt.ItemDataRole.UserRole) == 'r150'
    assert w.record_table.item(w.record_table.rowAt(0), 0).data(Qt.ItemDataRole.UserRole) == anchor


def test_evicted_selected_record_does_not_point_at_another_row(window, monkeypatch):
    w, _ = window
    w.navigate(PageId.RECORDS, RecordView.NOTIFICATIONS)
    rows = records(2)
    w._apply_records(rows)
    w.record_table.setCurrentCell(1, 0)
    w._apply_records(rows[:1])
    assert w.record_table.currentRow() == -1
    dialog = Mock()
    monkeypatch.setattr('deal_watch.ui.main.RecordDetailDialog', dialog)
    w.show_record_detail('notifications')
    dialog.assert_not_called()


def test_query_failure_keeps_existing_rows_and_offers_retry(window):
    w, _ = window
    w.navigate(PageId.RECORDS, RecordView.NOTIFICATIONS)
    w._apply_records(records(1))
    w._record_query_failed(RecordView.NOTIFICATIONS, 'OperationalError: database is locked')
    assert w.record_table.rowCount() == 1
    assert '数据库正忙' in w.records_count_label.text()
    assert not w.records_retry_button.isHidden()
    w.records_retry_button.click()
    assert w.records_refresh.request.call_args.kwargs['force'] is True
    w._apply_records(records(1))
    assert w.records_retry_button.isHidden()


def test_actions_resolve_region_id_after_reordering(window, monkeypatch):
    w, _ = window
    first, second = ready_region('甲'), ready_region('乙')
    w.config.regions = [first, second]
    w._load_regions()
    w.config.regions.reverse()
    w._load_regions()
    chosen = []
    monkeypatch.setattr(w, 'preview_region', lambda: chosen.append(w.selected_region().id))
    w._region_row_action('preview', first.id)
    assert chosen == [first.id]


def test_keyword_save_cannot_race_region_config_transaction(window):
    w, _ = window
    w.keywords_edit.setPlainText('新词')
    w._saving = True
    w.save_keywords()
    assert w.config.keywords == []
    assert not w.config_store.path.exists()
    w._saving = False


def test_background_query_failure_updates_controls_only_on_ui_thread(window, monkeypatch):
    import threading

    from PySide6.QtCore import QThread
    w, app = window
    seen = []
    apply = w._record_query_failed
    def record(view, error):
        seen.append(QThread.currentThread())
        apply(view, error)
    monkeypatch.setattr(w, '_record_query_failed', record)
    worker = threading.Thread(target=lambda: w.records_refresh.failed.emit('OperationalError: database is locked'))
    worker.start()
    worker.join(1)
    deadline = time.monotonic() + 1
    while not seen and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.005)
    assert seen == [app.thread()]
