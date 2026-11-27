"""Execute the actual Studio handler closures with controlled UI/service boundaries."""
import ast
from pathlib import Path
from types import SimpleNamespace as N

import pytest


SOURCE = Path(__file__).resolve().parents[2] / "src/carr_library/ui/studio_dialog.py"


class Choice:
    def __init__(self, value):
        self.value = value

    def GetSelection(self):
        return self.value

    GetValue = GetSelection

    def SetSelection(self, value):
        self.value = value

    SetValue = ChangeValue = SetSelection


def fixture():
    rows, calls, reports = [], [], []
    state = N(confirm=True, fail=False, selection=0)
    project = N(id="p", original_script="Synthetic", title="Test")

    class Dialog:
        def __init__(self, *args):
            pass

        def ShowModal(self):
            return int(state.confirm)

        def GetPath(self):
            return "/synthetic"

        def GetSelection(self):
            return state.selection

        def Destroy(self):
            pass

    def submit(p, path, **options):
        if state.fail:
            raise OSError("Synthetic output failure")
        row = dict(id=str(len(rows)), project={"title": "Test"}, status="complete",
                   progress=100, error="", **options)
        rows.append(row)
        return row["id"]

    def run_job(identity, **kwargs):
        calls.append(identity)
        return {"destination": "synthetic"}

    service = N(submit=submit, run=run_job, get=lambda i: rows[int(i)],
                jobs=lambda: rows, projects=N(load=lambda i: project))
    ns = dict(wx=N(DirDialog=Dialog, SingleChoiceDialog=Dialog, ID_OK=1),
              Path=Path, dialog=None, service=service, save=lambda: project,
              format_choice=Choice(0), stems=Choice(True), formats=["wav", "flac"],
              cancel=N(is_set=lambda: False), report=lambda *a: reports.append(a),
              Project=N(from_dict=lambda _: project), controller=N(project=project),
              editor=Choice("Synthetic"), title=Choice("Test"), controls=lambda: None,
              run=lambda op, done, **kw: done(op(lambda _: None)))
    source = SOURCE.read_text(encoding="utf-8")
    outer = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef)
                 and n.name == "show_studio")
    methods = [n for n in outer.body if isinstance(n, ast.FunctionDef)
               and n.name in {"restore_output_options", "render", "jobs"}]
    lines = source.splitlines(keepends=True)
    excerpt = "\n".join("".join(lines[n.lineno - 1:n.end_lineno]) for n in methods)
    exec("def factory():\n    current_job = None\n    busy = False\n" + excerpt
         + "\n    return render, jobs\n", ns)
    render, jobs = ns["factory"]()
    return N(**ns, render=render, jobs=jobs, rows=rows, calls=calls, reports=reports, state=state)


def test_unchanged_resume():
    f = fixture()
    f.render()
    f.render()
    assert f.calls == ["0", "0"] and len(f.rows) == 1


@pytest.mark.parametrize("index,stems", [(1, True), (0, False), (1, False)])
def test_changed_options_create_new_job(index, stems):
    f = fixture()
    f.render()
    f.format_choice.SetSelection(index)
    f.stems.SetValue(stems)
    f.render()
    assert f.calls == ["0", "1"]
    assert (f.rows[0]["format"], f.rows[0]["stems"]) == ("wav", True)
    assert (f.rows[1]["format"], f.rows[1]["stems"]) == (f.formats[index], stems)


def test_saved_job_restores_options():
    f = fixture()
    f.format_choice.SetSelection(1)
    f.stems.SetValue(False)
    f.render()
    f.format_choice.SetSelection(0)
    f.stems.SetValue(True)
    f.jobs()
    assert (f.format_choice.GetSelection(), f.stems.GetValue()) == (1, False)
    assert "Format: flac; stems: no" in f.reports[-1][0]
    f.render()
    assert f.calls == ["0", "0"]


@pytest.mark.parametrize("failure", [False, True])
def test_cancel_or_failed_new_job_preserves_previous(failure):
    f = fixture()
    f.render()
    f.format_choice.SetSelection(1)
    f.stems.SetValue(False)
    f.state.confirm = failure
    f.state.fail = failure
    f.render()
    assert len(f.rows) == 1 and f.calls == ["0"]
    assert (f.format_choice.GetSelection(), f.stems.GetValue()) == (0, True)
    f.state.confirm, f.state.fail = True, False
    f.render()
    assert f.calls == ["0", "0"]
