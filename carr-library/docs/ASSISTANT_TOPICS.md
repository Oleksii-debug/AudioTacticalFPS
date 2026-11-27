# Scoped topics in the current assistant

The existing assistant now offers three typed topic actions alongside its thirteen prior tools. The native assistant, CLI instructions, optional local-model proposal, and inert named workflow templates use the same Plan, AssistantService and JobStore. There is no alternate catalog, assistant service, or backup namespace.

## Review and run

Select the current library and an existing output folder outside private application data. Preview the complete plan before choosing Run. The CLI follows the same flow: its default invocation previews; a separate explicit --run submits the reviewed instructions.

~~~text
Create topic "Parent"
Create topic "Child" under "@0"
List topics
Export topic "@1" as "child-evidence.md"
~~~

The corresponding Ukrainian commands are:

~~~text
Створи тему "Parent"
Створи тему "Child" у "@0"
Покажи теми
Експортуй тему "@1" як "child-evidence.md"
~~~

@N is the zero-based index of an earlier topic.create step in that exact plan. It cannot refer to a later step, an earlier search/list result, or another workspace. Combining selected named templates rebases each template's local references to its actual offset, without modifying either saved draft.

| Tool | Exact arguments | Result |
| --- | --- | --- |
| topic.list | none | Visible topics in the selected workspace |
| topic.create | name; optional parent identity or prior @N | One deterministic, transactionally created topic |
| topic.export | topic identity or prior @N; name; optional format | One new file rendered from existing saved verified evidence |

Supported formats are the existing markdown, text, ris, bibtex and csv renderers. Names remain relative to the reviewed output directory. Existing files are refused before their contents are read. Topic exports never approve replacement, create arbitrary parent directories, change source evidence, or reinterpret research notes as quotations.

The exporter calls LibraryApp.export_topic. Exact source spans, original locators and metadata, and separate research commentary remain under the current evidence renderer. Content is captured at execution time. The publication intent freezes its actual byte length and SHA256; a resumed preparation refuses changed evidence. A completed result remains a historical export and does not silently re-render it from later notes.

## Durable ownership and recovery

Topic creation uses a deterministic job-and-step UUID in the current research_topic table, inside the existing authorized TopicTree transaction. A killed process before commit leaves no topic; a killed process after commit can confirm only the same name, parent, visibility and order. A changed or archived topic requires a new review.

Topic export reuses research.export_file.write_export and its existing atomic no-clobber publication. An additive, optional callback runs after the temporary file is flushed and fsynced and before publication. The current assistant journal then records the exact destination parent identity, temporary basename and file identity, size and hash.

Resume accepts an existing target only if its file identity matches that recorded temporary file. On POSIX, a crash can leave the recorded temporary and target as exactly two links to the same inode. Both paths and bytes must match before only the owned temporary link is removed. Unknown extra links, a replaced parent, replaced stages, edited content, or an independent same-byte target all refuse recovery without overwriting or deleting those files. Windows keeps the existing atomic rename path.

Cancellation before publication leaves no output. Once publication completes, a late Stop does not turn success into a retry. An owned post-commit cleanup or directory-sync problem is retained as a visible completion notice. These rules do not promise power-loss recovery when the operating system has not confirmed directory durability.

The added shared publisher parameters are optional; all old call defaults remain unchanged. The current pre-read destination identity and Windows ChangeTime checks, full digest verification and source-cancellation guards are preserved.

## Backup and restored approval

Current typed assistant jobs and templates remain under the same existing backup adapter. Pure structural validation checks topic arguments, prior references, deterministic results and exact publication intents without opening external saved paths. Backup source validation also checks that the recorded topic exists in the recorded workspace; archiving a topic does not erase its historical job.

Complete restore preserves those records and the existing topic/evidence catalog. A restored job has no authority to replay its old external export. Copying or loading its steps produces an inert draft, which needs a current scope, a new Preview and a new Run. This component does not activate the canonical alternate assistant's historical job format; historical envelopes remain under the separate passive recovery bridge.

## Qualification

The declared inventory is 94 source and 94 isolated installed cases, including 16 actual subprocess-crash cases. The crash tests terminate a real isolated process around SQLite commit, fsynced preparation, native rename/link publication and the action/checkpoint boundary. They also exercise foreign same-byte files, changed parents, extra/replaced links, altered evidence and a completed export whose owned-link cleanup fails. All thirteen prior tools remain allowlisted and their existing suites remain required.

The native probe uses the actual current wx assistant dialog to review and execute five steps, create two scoped topics, publish two files, show results, settle its worker, and verify typed backup plus passive recovery. It requires 23 exact check labels, six exact measurements and ten installed module hashes matching the candidate wheel. The source and native declarations are stored in scripts/topic_tools_oracle.json.

These tests and this native probe are authored and source-reviewed, but their new execution is pending. They are not evidence of NVDA qualification, language-model quality or physical audio operation.
