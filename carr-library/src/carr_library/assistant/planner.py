"""Deterministic guided commands; no claims of general-language understanding."""
import json
import re
from .models import Plan, Step, text


HELP = '''Enter one command per line (English or Ukrainian), or a versioned JSON plan.
Create library "History" / Створи бібліотеку "Історія"
Import "C:/Books/source.txt" / Імпортуй "C:/Books/source.txt"
Import folder "C:/Books" / Імпортуй папку "C:/Books"
Search "railway transport" / Знайди "залізничний транспорт"
List books / Покажи книги
List libraries / Покажи бібліотеки
Extract "EDITION_ID" as "reading.txt" / Експортуй текст "EDITION_ID" як "reading.txt"
Backup "backup-name" / Резервна копія "backup-name"
Convert "C:/Books/source.docx" as "reading.txt" / Конвертуй "C:/Books/source.docx" як "reading.txt"
Effects "C:/Scripts/source.txt" as "edited.txt" for "Narrator": [{"op":"add","effect":{"kind":"gain","parameters":[["gain_db",-3]],"enabled":true}}]
Use "master" as the effects target for an explicit master chain or dry/wet mix.
File derivatives need selected input/output folders, a source checksum and two new output files.
Conversion uses the existing local readers. Optional converter/OCR selections remain in Document conversion.
Native reader.control/player.control plans are prepared from the assistant action inside the selected reader.
Their receipts describe observed commands; playback start does not claim that the whole book was read.
Produce audio: [script text] / Озвуч: [текст сценарію]
Audio prose is literal narration. Only an explicit JSON audio.produce step with input_kind="script"
or a typed script object interprets role/music/pause directions.
Paths must be inside your selected input/output folders. No delete, shell or remote tools are available.'''


def preview(value, scope):
    text(value, 'assistant instruction', maximum=4 * 1024 * 1024)
    if value.lstrip().startswith('{'):
        return Plan.from_json(value, scope=scope)
    steps = []
    lines = value.strip().splitlines()
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        index += 1
        if not line:
            continue
        # The quoted payload is data. In particular, it is never re-parsed as commands.
        patterns = (
            (r'(?:create\s+(?:a\s+)?library|створи\s+бібліотеку)\s+"([^"]+)"\.?', 'library.create', 'name'),
            (r'(?:import\s+folder|імпортуй\s+папку)\s+"([^"]+)"\.?', 'library.import_folder', 'path'),
            (r'(?:import|імпортуй)\s+"([^"]+)"\.?', 'library.import', 'path'),
            (r'(?:search(?:\s+for)?|знайди|пошук)\s+"([^"]+)"\.?', 'library.search', 'query'),
            (r'(?:backup|резервна\s+копія)\s+"([^"]+)"\.?', 'library.backup', 'name'),
        )
        matched = False
        for pattern, tool, key in patterns:
            match = re.fullmatch(pattern, line, re.IGNORECASE)
            if match:
                steps.append(Step(tool, {key: match.group(1)}))
                matched = True
                break
        if matched:
            continue
        if re.fullmatch(r'(?:list\s+(?:all\s+)?libraries|покажи\s+бібліотеки)\.?', line, re.IGNORECASE):
            steps.append(Step('library.list'))
            continue
        if re.fullmatch(r'(?:list\s+books|покажи\s+книги)\.?', line, re.IGNORECASE):
            steps.append(Step('library.catalog'))
            continue
        match = re.fullmatch(r'(?:convert|конвертуй)\s+"([^"]+)"\s+(?:as|як)\s+"([^"]+)"\.?', line, re.IGNORECASE)
        if match:
            from .derived_tools import capture_source
            source, record = capture_source(match.group(1), scope)
            steps.append(Step('document.convert', {'path': str(source),
                'source_sha256': record['sha256'], 'name': match.group(2)}))
            continue
        match = re.fullmatch(r'(?:effects|ефекти)\s+"([^"]+)"\s+(?:as|як)\s+"([^"]+)"\s+(?:for|для)\s+"([^"]+)"\s*:\s*(.+)', line, re.IGNORECASE)
        if match:
            from .derived_tools import capture_source, MAX_SCRIPT
            source, record = capture_source(match.group(1), scope, maximum=MAX_SCRIPT)
            target = None if match.group(3).casefold() in ('master', 'майстер') else match.group(3)
            steps.append(Step('audio.effects', {'path': str(source), 'source_sha256': record['sha256'],
                'name': match.group(2), 'target': target, 'changes': json.loads(match.group(4))}))
            continue
        match = re.fullmatch(r'(?:extract|експортуй\s+текст)\s+"([^"]+)"\s+(?:as|як)\s+"([^"]+)"\.?', line, re.IGNORECASE)
        if match:
            steps.append(Step('library.extract', {'edition': match.group(1), 'name': match.group(2)}))
            continue
        match = re.match(r'(?:produce\s+audio|озвуч|створи\s+аудіо)\s*:\s*(.*)', line, re.IGNORECASE)
        if match:
            script = '\n'.join([match.group(1), *lines[index:]]).strip()
            text(script, 'audio script', maximum=4 * 1024 * 1024)
            steps.append(Step('audio.produce', {'text': script}))
            break
        raise ValueError(f'Command {index} is ambiguous or unsupported. Use the guided command examples; nothing has run.')
    summary = '\n'.join(f'{i + 1}. {s.tool}: ' + json.dumps(s.arguments, ensure_ascii=False)
                        for i, s in enumerate(steps))
    return Plan(scope, tuple(steps), summary[:16384])
