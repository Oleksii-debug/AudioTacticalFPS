[CmdletBinding()]
param(
  [ValidateRange(0,3)][int]$Shard = 0,
  [string]$ExpectedHead = '',
  [switch]$InventoryOnly
)
$ErrorActionPreference = 'Stop'

$suites = @(
  'tests/e2e/test_import_progress.py',
  'tests/speech/test_progress_speech.py',
  'tests/studio/test_production_cancellation_outcomes.py',
  'tests/ui/test_progress_numeric_limits.py',
  'tests/ui/test_studio_progress_isolation.py',
  'tests/ui/test_progress_media_quiet.py',
  'tests/ui/test_progress_voice_actions.py',
  'tests/storage/test_progress_preferences_backup.py',
  'tests/progress/test_progress_media_integration.py',
  'tests/packaging/test_offline_runtime_policy.py',
  'tests/packaging/test_install_bytecode.py',
  'tests/e2e/test_offline_startup.py',
  'bootstrap/semantic_compat/test_probe.py',
  'tests/e2e/test_application.py',
  'tests/e2e/test_result_presentation.py',
  'tests/e2e/test_import_batches.py',
  'tests/ingest/test_batch_scan.py',
  'tests/ui/test_import_center.py',
  'tests/ui/test_ocr_model_manager.py',
  'tests/ui/test_ocr_selection.py',
  'tests/ui/test_ocr_options.py',
  'tests/e2e/test_epub_import.py',
  'tests/extract/test_document_formats_text.py',
  'tests/extract/test_document_formats_xml.py',
  'tests/e2e/test_extended_document_workflows.py',
  'tests/extract/test_epub_ordered.py',
  'tests/reader/test_ebook_reader.py',
  'tests/ui/test_ebook_reader.py',
  'tests/extract/test_epub_navigation.py',
  'tests/reader/test_ebook_contents.py',
  'tests/e2e/test_epub_library.py',
  'tests/e2e/test_epub_reading_desk.py',
  'tests/ui/test_ebook_dialog.py',
  'tests/e2e/test_retained_source_search_integrity.py',
  'tests/e2e/test_research_workflows.py',
  'tests/e2e/test_catalog_insights_workflow.py',
  'tests/e2e/test_notebook_research_workflow.py',
  'tests/e2e/test_advanced_research_search.py',
  'tests/e2e/test_current_topic_scope.py',
  'tests/e2e/test_search_operation_budget.py',
  'tests/e2e/test_nested_search_clock_budget.py',
  'tests/ingest/test_application_atomic_gate.py',
  'tests/ingest/test_worker_isolated_launch.py',
  'tests/ingest/test_application_atomic_docx_gate.py',
  'tests/e2e/test_semantic_fallback_status.py',
  'tests/search/test_semantic_cache.py',
  'tests/search/test_semantic_cache_boundary.py',
  'tests/e2e/test_source_file.py',
  'tests/e2e/test_export_destination.py',
  'tests/e2e/test_academic_document_export.py',
  'tests/e2e/test_data_directory_containment.py',
  'tests/extract/test_clipping_visibility.py',
  'tests/extract/test_ocr_model_catalog.py',
  'tests/storage/test_library_backup.py',
  'tests/storage/test_library_backup_reading_state.py',
  'tests/catalog/test_metadata_backup_roundtrip.py',
  'tests/reader/test_context.py',
  'tests/reader/test_topic_tree.py',
  'tests/storage/test_backup_schema_identity.py',
  'tests/storage/test_academic_migration.py',
  'tests/research/test_academic_library.py',
  'tests/research/test_csl_author_boundaries.py',
  'tests/e2e/test_academic_workflow.py',
  'tests/citations/test_finish_label_integrity.py',
  'tests/storage/test_printed_label_integrity.py',
  'tests/catalog/test_catalog_panel_acceptance.py',
  'tests/catalog/test_bibliography.py',
  'tests/catalog/test_catalog_reports.py',
  'tests/e2e/test_catalog_tools.py',
  'tests/acquisition/test_providers.py',
  'tests/acquisition/test_acquisition_security.py',
  'tests/acquisition/test_acquisition_workflow.py',
  'tests/acquisition/test_acquisition_cli.py',
  'tests/ui/test_native_acquisition.py',
  'tests/research/test_review_matrix.py',
  'tests/ui/test_catalog_tools_dialog.py',
  'tests/catalog/test_insights_budget.py',
  'tests/storage/test_v2_recovery.py',
  'tests/storage/test_catalog_composition.py',
  'tests/reader/test_saved_topic_query.py',
  'tests/reader/test_evidence_export.py',
  'tests/reader/test_file_export.py',
  'tests/reader/test_academic_documents.py',
  'tests/reader/test_bibliographic_locators.py',
  'tests/reader/test_binary_file_export.py',
  'tests/reader/test_literature_matrix.py',
  'tests/reader/test_topic_search_runtime.py',
  'tests/research/test_notebook_search.py',
  'tests/reader/test_topic_literature_matrix.py',
  'tests/reader/test_outline_export.py',
  'tests/catalog/test_ris_apply.py',
  'tests/catalog/test_quality.py',
  'tests/ui/test_academic_tools.py',
  'tests/ui/test_literature_matrix.py',
  'tests/ui/test_bibliography_import.py',
  'tests/ui/test_catalog_quality.py',
  'tests/reader/test_reading_desk.py',
  'tests/reader/test_reading_state.py',
  'tests/search/test_literal_long_source.py',
  'tests/search/test_thematic_work_diversity.py',
  'tests/search/test_literal_v2_composition.py',
  'tests/search/test_advanced_search.py',
  'tests/ui/test_desktop_query_jobs.py',
  'tests/ui/test_scope_labels.py',
  'tests/ui/test_desktop_handlers.py',
  'tests/ui/test_research_actions.py',
  'tests/ui/test_academic_export_handler.py',
  'tests/ui/test_notebook_search_actions.py',
  'tests/ui/test_reading_desk_actions.py',
  'tests/ui/test_desktop_result_modes.py',
  'tests/ui/probe_advanced_query_builder.py',
  'tests/ui/test_reader_command_dispatch.py',
  'tests/ui/test_reader_keymap_context.py',
  'tests/ui/test_command_registry.py',
  'tests/ui/test_keyboard_manager.py',
  'tests/storage/test_keyboard_preferences.py',
  'tests/storage/test_evidence_drafts.py',
  'tests/ui/test_evidence_draft_recovery.py',
  'tests/ui/probe_workspace_shell.py',
  'tests/ui/probe_catalog_insights_actions.py',
  'tests/ui/test_academic_library_dialog.py',
  'tests/accessibility/test_pr71_catalog_scope_ux.py',
  'tests/ui/test_source_open_handler.py',
  'tests/catalog/test_reference_exchange.py',
  'tests/research/test_outline_export.py',
  'tests/e2e/test_academic_workflows.py',
  'tests/ui/test_academic_actions.py',
  'tests/e2e/test_research_search_cli.py',
  'tests/reader/test_query_manager.py',
  'tests/ui/test_query_manager_actions.py',
  'tests/search/test_federated.py',
  'tests/search/test_federated_independent.py',
  'tests/ui/test_federated_search.py',
  'tests/search/test_federated_epub.py',
  'tests/storage/test_saved_query_v4_upgrade.py',
  'tests/e2e/test_research_workflow_roundtrip.py',
  'tests/citations/test_authorized_action.py',
  'tests/citations/test_finish_pdf_authority.py',
  'tests/citations/test_pdf_mapping_contradiction.py',
  'tests/storage/test_research_schema.py',
  'tests/packaging/test_source_wheel.py',
  'tests/packaging/test_ci_check.py',
  'tests/catalog/test_application_metadata_integrity.py',
  'tests/storage/test_finish_workspace_integrity.py',
  'tests/extract/test_docx_hidden_styles.py',
  'tests/packaging/test_windows_bundle.py',
  'tests/packaging/test_local_audio_setup.py',
  'tests/ingest/test_import_metadata_shape.py',
  'tests/library_assistant/test_library_workflow.py',
  'tests/library_assistant/test_library_workflow_ui.py',
  'tests/ingest/test_txt_replay_registration.py',
  'tests/ui/test_federated_labels.py',
  'tests/search/test_literal_unicode_memory.py',
  'tests/e2e/test_search_deadline.py',
  'tests/ingest/test_terminal_replay_registration.py',
  'tests/assistant/test_backup_receipt.py',
  'tests/integrations/test_dordiai_voices.py',
  'tests/packaging/test_delivery_archive.py',
  'tests/ui/test_save_all.py',
  'tests/ui/test_studio_keyboard_registration.py',
  'tests/voice_acceptance/test_dordiai_acceptance.py',
  'tests/integrations/test_dordiai_speech.py',
  'tests/integrations/test_dordiai_settings.py',
  'tests/integrations/test_dordiai_convergence.py',
  'tests/integrations/test_dordiai_probe_cancellation.py',
  'tests/ui/test_studio_progress_results.py',
  'tests/ui/test_worker_cleanup.py',
  'tests/assistant/test_local_assistant.py',
  'tests/assistant/test_supplied_transcription.py',
  'tests/speech/test_native_sapi_protocol.py',
  'tests/speech/test_output_fsync.py',
  'tests/speech/test_reading.py',
  'tests/speech/test_session.py',
  'tests/speech/test_source.py',
  'tests/audio/test_audio_process.py',
  'tests/audio/test_publication_fsync.py',
  'tests/studio/test_music_scheduling.py',
  'tests/studio/test_studio_controller.py',
  'tests/ingest/test_import_publication_point.py',
  'tests/ingest/test_import_replay_identity.py',
  'tests/reader/test_export_no_clobber.py',
  'tests/ui/test_citation_styles_jobs.py',
  'tests/research/test_bibtex_import.py',
  'tests/ui/test_progress_job_results.py',
  'tests/ui/test_progress_panel_state.py',
  'tests/storage/test_state_snapshot_identity.py',
  'tests/storage/test_backup_generated_audio.py',
  'tests/storage/test_backup_translation.py',
  'tests/assistant/test_translation.py',
  'tests/assistant/test_translation_local.py',
  'tests/assistant/test_translation_documents.py',
  'tests/ui/test_translation_documents.py',
  'tests/packaging/test_native_checks_manifest.py',
  'tests/test_delivery_contract.py',
  'tests/storage/test_backup_primary_producers.py',
  'tests/storage/test_backup_retained_audio.py',
  'tests/storage/test_state_json_reader.py',
  'tests/storage/test_engine_settings_backup.py',
  'tests/storage/test_engine_settings_dordiai_backup.py',
  'tests/audio/test_bundled_runtime_settings.py',
  'tests/progress/test_progress_contract.py',
  'tests/progress/test_progress_feedback.py',
  'tests/progress/test_progress_preferences.py',
  'tests/progress/test_progress_reporting_failure.py',
  'tests/language_qualification/test_catalog.py',
  'tests/language_qualification/test_ui.py',
  'tests/citations/test_csl.py',
  'tests/citations/test_custom_csl.py',
  'tests/citations/test_bibliography_workbench.py',
  'tests/citations/test_custom_workbench.py',
  'tests/ingest/test_worker_publication_boundary.py',
  'tests/e2e/test_import_files_cancellation.py',
  'tests/storage/test_complete_pending_backup.py',
  'tests/storage/test_backup_pending_common4.py',
  'tests/storage/test_pending_backup.py',
  'tests/reader/test_export_publication_boundary.py',
  'tests/reader/test_export_change_time.py',
  'tests/ui/test_export_confirmation.py',
  'tests/storage/test_backup_v4_sidecars.py',
  'tests/storage/test_v3_state_backup.py',
  'tests/e2e/test_import_worker_publication.py',
  'tests/packaging/test_worker_bytecode.py',
  'tests/studio/test_clip_editing.py',
  'tests/studio/test_clip_restoration.py',
  'tests/studio/test_clip_dialog.py',
  'tests/studio/test_clip_cli.py',
  'tests/storage/test_state_clip_inputs.py',
  'tests/audio/test_recording.py',
  'tests/audio/test_recording_cleanup.py',
  'tests/audio/test_recording_compatibility.py',
  'tests/audio/test_recording_export.py',
  'tests/ui/test_recording_dialog.py',
  'tests/audio/test_dsp.py',
  'tests/audio/test_effect_presets.py',
  'tests/audio/test_audio_conversion.py',
  'tests/ui/test_audio_conversion.py',
  'tests/speech/test_runtime.py',
  'tests/audio/test_windows_audio_ownership.py',
  'tests/packaging/test_prepare_windows_audio.py',
  'tests/studio/test_publishing.py',
  'tests/studio/test_publishing_cli.py',
  'tests/studio/test_publishing_handlers.py',
  'tests/assistant/test_translation_v3.py',
  'tests/assistant/test_translation_advanced_source.py',
  'tests/assistant/test_translation_advanced_union.py',
  'tests/assistant/test_local_planning.py',
  'tests/assistant/test_planning_handlers.py',
  'tests/assistant/test_workflow_templates.py',
  'tests/assistant/test_workflow_template_handlers.py',
  'tests/studio/test_book_audio.py',
  'tests/ui/test_book_audio_dialog.py',
  'tests/ui/test_worker_lifetime.py',
  'tests/storage/test_backup_book_audio.py',
  'tests/storage/test_backup_studio_draft.py',
  'tests/storage/test_backup_effect_presets.py',
  'tests/e2e/test_import_worker.py',
  'tests/search/test_federated_advanced.py',
  'tests/storage/test_backup_v4_review.py',
  'tests/packaging/test_native_checks.py',
  'tests/packaging/test_refresh_private_bundle_review.py',
  'tests/e2e/test_pdf_review_cancellation.py',
  'tests/storage/test_crash_backup.py',
  'tests/speech/test_python_helper.py',
  'tests/ui/test_operation_progress.py',
  'tests/ui/test_progress_job_review.py',
  'tests/transcription/test_supplied_worker_bridge.py',
  'tests/transcription/test_v5_worker_profile.py',
  'tests/transcription/test_runtime_interpreter.py',
  'tests/transcription/test_protocol_fixture.py',
  'tests/transcription/test_protocol_fixture_windows.py',
  'tests/transcription/test_service.py',
  'tests/transcription/test_admission_boundaries.py',
  'tests/transcription/test_runtime_admission.py',
  'tests/transcription/test_restore.py',
  'tests/storage/test_transcription_backup.py',
  'tests/ui/test_transcription_dialog.py',
  'tests/e2e/test_transcription_cli.py',
  'tests/transcription/test_unified_authority.py',
  'tests/transcription/test_export_publication.py',
  'tests/ingest/test_initial_publication_completion.py',
  'tests/ui/test_export_destination.py',
  'tests/storage/test_durable_state_publication.py',
  'tests/ui/test_profile_project_exports.py',
  'tests/speech/test_reader_usability.py',
  'tests/speech/test_navigation.py',
  'tests/reader/test_ebook_audio.py',
  'tests/ui/test_ebook_audio_lifetime.py',
  'tests/operations/test_worker_start_cleanup.py',
  'tests/speech/test_player_controls.py',
  'tests/speech/test_player_transport.py',
  'tests/ui/test_reader_playback.py',
  'tests/ui/test_reader_playback_actions.py',
  'tests/packaging/test_audio_runtime.py',
  'tests/packaging/test_audio_bundle.py',
  'tests/reader/test_export_reconciliation.py',
  'tests/storage/test_incremental_backup.py',
  'tests/storage/test_incremental_backup_cli.py',
  'tests/sync/test_vault.py',
  'tests/ui/test_incremental_backup_dialog.py',
  'tests/audio/test_audiobook.py',
  'tests/studio/test_audiobook_publication.py',
  'tests/studio/test_daisy_publishing.py',
  'tests/studio/test_daisy_boundaries.py',
  'tests/studio/test_publishing_boundaries.py',
  'tests/storage/test_backup_publication_formats.py',
  'tests/packaging/test_managed_launch_boundary.py',
  'tests/packaging/test_managed_update_native_policy.py',
  'tests/packaging/test_native_launcher_bundle.py',
  'tests/packaging/test_standard_installer.py',
  'tests/security/test_anchor_locking.py',
  'tests/security/test_canonical_boundary.py',
  'tests/security/test_controller.py',
  'tests/security/test_entitlements.py',
  'tests/security/test_idle_renewal.py',
  'tests/security/test_independent_review_boundaries.py',
  'tests/security/test_installation.py',
  'tests/security/test_managed_installation.py',
  'tests/security/test_managed_registration.py',
  'tests/security/test_publication_integrity.py',
  'tests/security/test_refresh.py',
  'tests/security/test_updates.py',
  'tests/ui/test_trust_status_handlers.py',
  'tests/security/test_windows_vault.py',
  'tests/ui/test_find_replace_hooks.py',
  'tests/ui/test_reader_navigation_ui.py',
  'tests/ui/test_find_replace_ui.py',
  'tests/ui/test_find_replace.py',
  'tests/operations/test_facade.py',
  'tests/operations/test_import_admission.py',
  'tests/operations/test_production_admission.py',
  'tests/operations/test_resources.py',
  'tests/operations/test_transcription_admission.py',
  'tests/operations/test_current_union.py',
  'tests/ui/test_operations_dialog.py',
  'tests/ui/test_operations_policy.py',
  'tests/e2e/test_operations_cli.py',
  'tests/reader/test_braille.py',
  'tests/ui/test_braille_actions.py',
  'tests/studio/test_project_effects.py',
  'tests/storage/test_backup_studio_effects.py',
  'tests/integrations/test_dordiai_host_lifecycle.py',
  'tests/ui/test_engine_settings_lifetime.py',
  'tests/speech/test_listening.py',
  'tests/speech/test_playback_devices.py',
  'tests/ui/test_reader_listening_hooks.py',
  'tests/packaging/test_installed_trusted_client_probe.py',
  'tests/speech/test_wave_output.py',
  'tests/speech/test_playlist.py',
  'tests/speech/test_playlist_recovery.py',
  'tests/ui/test_audio_playback.py',
  'tests/audio/test_conversion_batch.py',
  'tests/ui/test_audio_conversion_batch.py',
  'tests/studio/test_conversion_batch_cli.py',
  'tests/speech/test_publication_validation.py',
  'tests/speech/test_reading_marks.py',
  'tests/ui/test_reading_tools.py',
  'tests/speech/test_reading_tabs.py',
  'tests/ui/test_reader_tabs.py',
  'tests/speech/test_reading_presentation.py',
  'tests/speech/test_reading_presentation_handlers.py',
  'tests/speech/test_reading_presentation_reader_hook.py',
  'tests/ui/test_native_reading_presentation_driver.py',
  'tests/speech/test_voice_preferences.py',
  'tests/storage/test_voice_preferences_backup.py',
  'tests/ui/test_voice_preferences_dialog.py',
  'tests/ui/test_reader_view_hooks.py',
  'tests/storage/test_reader_preferences_state.py',
  'tests/studio/test_book_batch.py',
  'tests/studio/test_book_cli.py',
  'tests/ui/test_book_batch_dialog.py',
  'tests/storage/test_backup_book_batches.py',
  'tests/audio/test_recording_controls.py',
  'tests/audio/test_recording_monitor.py',
  'tests/storage/test_backup_recording_controls.py',
  'tests/catalog/test_streaming_intake.py',
  'tests/e2e/test_raster_import.py',
  'tests/e2e/test_raster_ocr_native.py',
  'tests/extract/test_raster_pages.py',
  'tests/ingest/test_batch_continuation.py',
  'tests/ingest/test_batch_continuation_cli.py',
  'tests/search/test_search_paging.py',
  'tests/ingest/test_scan_root_binding.py',
  'tests/ingest/test_large_source_intake.py',
  'tests/storage/test_desktop_preferences.py',
  'tests/ui/test_desktop_integration.py',
  'tests/ui/test_desktop_settings.py',
  'tests/ui/test_desktop_host_lifecycle.py',
  'tests/speech/test_narration_receipt.py',
  'tests/storage/test_local_schedules.py',
  'tests/ui/test_local_schedules.py',
  'tests/storage/test_backup_local_schedules.py',
  'tests/packaging/test_local_schedules_cli.py',
  'tests/speech/test_spelling.py',
  'tests/speech/test_spelling_handlers.py',
  'tests/speech/test_spelling_reader_hook.py',
  'tests/packaging/test_hunspell_build.py',
  'tests/packaging/test_library_scale_benchmark.py',
  'tests/audio/test_envelopes.py',
  'tests/studio/test_envelope_workflow.py',
  'tests/studio/test_clip_operations.py',
  'tests/studio/test_project_history.py',
  'tests/studio/test_clip_history_dialog.py',
  'tests/assistant/test_translation_layout.py',
  'tests/ui/test_translation_layout.py',
  'tests/assistant/test_translation_layout_cli.py',
  'tests/assistant/test_translation_partition.py',
  'tests/academic_integration/test_word_records.py',
  'tests/academic_integration/test_word_bridge.py',
  'tests/academic_integration/test_word_worker.py',
  'tests/ui/test_word_citations.py',
  'tests/assistant/test_google_oauth.py',
  'tests/ui/test_google_oauth.py',
  'tests/assistant/test_translation_epub_layout.py',
  'tests/ui/test_translation_epub_layout.py',
  'tests/assistant/test_translation_epub_cli.py',
  'tests/studio/test_clip_copy.py',
  'tests/studio/test_clip_copy_dialog.py',
  'tests/extract/test_docx_rich.py',
  'tests/assistant/test_translation_rich_docx.py',
  'tests/ui/test_translation_rich_docx.py',
  'tests/assistant/test_translation_rich_docx_cli.py',
  'tests/extract/test_docx_ordered.py',
  'tests/extract/test_docx_omitted_content.py',
  'tests/ui/test_native_recording_driver.py',
  'tests/storage/test_scoped_locator_capacity.py',
  'tests/storage/test_locator_integrity.py',
  'tests/ingest/test_checked_catalog_commit.py',
  'tests/search/test_search_query_work.py',
  'tests/operations/test_broker_scope.py',
  'tests/ingest/test_pipeline.py',
  'tests/storage/test_schema_boundary.py',
  'tests/storage/test_revisions.py',
  'tests/storage/test_read_boundary.py',
  'tests/catalog/test_read_views.py',
  'tests/catalog/test_read_views_v2_integration.py',
  'tests/search/test_literal_integrity.py',
  'tests/search/test_literal_readonly.py',
  'tests/packaging/test_library_capacity_measurement.py',
  'tests/storage/test_backup_capacity.py',
  'tests/storage/test_backup_capacity_cli.py',
  'tests/ui/test_backup_capacity.py',
  'tests/studio/test_book_effects.py',
  'tests/studio/test_measured_audio.py',
  'tests/studio/test_measured_pcm.py',
  'tests/studio/test_book_measured_cli.py',
  'tests/ui/test_book_effects.py',
  'tests/ui/test_book_duration_dialog.py',
  'tests/packaging/test_owner_candidate_archives.py',
  'tests/conversion/test_selected_conversion.py',
  'tests/conversion/test_calibre_admission.py',
  'tests/conversion/test_office_admission.py',
  'tests/ui/test_document_conversion.py',
  'tests/transcription/test_native_profiles_fixture.py',
  'tests/ui/test_native_backup_capacity_driver.py',
  'tests/ui/test_native_studio_book_choices.py',
  'tests/ui/test_native_book_publication_driver.py',
  'tests/search/test_paged_candidate_work.py',
  'tests/audio/test_live_metadata_retry.py',
  'tests/ui/test_native_conversion_diagnostics.py',
    "tests/audio/test_voice_package_management.py",
    "tests/audio/test_voice_management_state.py",
    "tests/audio/test_voice_reference_management.py",
    "tests/audio/test_voice_protected_management.py",
    "tests/audio/test_voice_publication_lifetime.py",
    "tests/audio/test_voice_ownership.py",
    "tests/integrations/test_dordiai_owner_lease.py",
    "tests/integrations/test_dordiai_managed_authority.py",
    "tests/ui/test_voice_packages.py",
    "tests/ui/test_voice_reference.py",
    "tests/storage/test_state_backup_portability.py",
    "tests/search/test_window_catalog_projection.py",
    "tests/ui/test_native_saved_audio_cleanup.py",
  'tests/integrations/test_dordiai_reference_backends.py',
  'tests/integrations/test_dordiai_managed_backends.py',
  'tests/citations/test_attestation_cancellation.py',
  'tests/packaging/test_library_scale_profile.py',
  'tests/studio/test_daisy_schema_resolver.py',
  'tests/assistant/test_transcription_provider.py',
  'tests/transcription/test_remote_service.py',
  'tests/ui/test_transcription_remote.py',
  'tests/security/test_update_handoff.py',
  'tests/security/test_update_runtime.py',
  'tests/security/test_update_ui.py',
  'tests/security/test_update_controller_actions.py',
  'tests/audio/test_voice_scope_checks.py',
  'tests/catalog/test_text_screen_work.py',
  'tests/search/test_ascii_mapping_work.py',
  'tests/studio/test_clip_preview_loop.py',
  'tests/studio/test_project_markers.py',
  'tests/studio/test_marker_consumers.py',
  "tests/language_qualification/test_evidence.py",
  "tests/language_qualification/test_evidence_backup.py",
  "tests/language_qualification/test_evidence_ui.py"
)

$operationsCounts = @{
  'tests/operations/test_facade.py' = 22
  'tests/operations/test_import_admission.py' = 6
  'tests/operations/test_production_admission.py' = 18
  'tests/operations/test_resources.py' = 18
  'tests/operations/test_transcription_admission.py' = 10
  'tests/operations/test_current_union.py' = 11
  'tests/operations/test_worker_start_cleanup.py' = 2
  'tests/ui/test_operations_dialog.py' = 13
  'tests/ui/test_operations_policy.py' = 37
  'tests/e2e/test_operations_cli.py' = 5
}
$readerExpected = @{
  'tests/speech/test_reader_usability.py' = 33
  'tests/speech/test_navigation.py' = 27
  'tests/speech/test_player_controls.py' = 37
  'tests/speech/test_player_transport.py' = 11
  'tests/ui/test_reader_playback.py' = 52
  'tests/ui/test_reader_playback_actions.py' = 24
  'tests/ui/test_ebook_audio_lifetime.py' = 28
  'tests/ui/test_progress_voice_actions.py' = 22
}
$completedFeatureCounts = @{
  'tests/reader/test_braille.py' = 40
  'tests/ui/test_braille_actions.py' = 8
  'tests/studio/test_project_effects.py' = 25
  'tests/storage/test_backup_studio_effects.py' = 21
  'tests/integrations/test_dordiai_host_lifecycle.py' = 13
  'tests/ui/test_engine_settings_lifetime.py' = 43
  'tests/speech/test_listening.py' = 28
  'tests/speech/test_playback_devices.py' = 53
  'tests/ui/test_reader_listening_hooks.py' = 24
  'tests/ui/test_reader_playback.py' = 52
  'tests/ui/test_reader_playback_actions.py' = 24
  'tests/packaging/test_installed_trusted_client_probe.py' = 34
  'tests/studio/test_clip_dialog.py' = 45
  'tests/packaging/test_standard_installer.py' = 102
  'tests/speech/test_wave_output.py' = 31
  'tests/speech/test_playlist.py' = 21
  'tests/speech/test_playlist_recovery.py' = 36
  'tests/ui/test_audio_playback.py' = 24
  'tests/audio/test_conversion_batch.py' = 42
  'tests/ui/test_audio_conversion_batch.py' = 21
  'tests/studio/test_conversion_batch_cli.py' = 4
  'tests/speech/test_publication_validation.py' = 12
  'tests/speech/test_reading_marks.py' = 36
  'tests/ui/test_reading_tools.py' = 14
  'tests/speech/test_reading_tabs.py' = 25
  'tests/ui/test_reader_tabs.py' = 20
  'tests/speech/test_reading_presentation.py' = 32
  'tests/speech/test_reading_presentation_handlers.py' = 20
  'tests/speech/test_reading_presentation_reader_hook.py' = 9
  'tests/ui/test_native_reading_presentation_driver.py' = 14
  'tests/speech/test_voice_preferences.py' = 23
  'tests/storage/test_voice_preferences_backup.py' = 2
  'tests/ui/test_voice_preferences_dialog.py' = 12
  'tests/ui/test_progress_job_review.py' = 48
  'tests/ui/test_reader_navigation_ui.py' = 11
  'tests/ui/test_reader_view_hooks.py' = 11
  'tests/storage/test_reader_preferences_state.py' = 7
  'tests/studio/test_book_batch.py' = 29
  'tests/studio/test_book_cli.py' = 18
  'tests/ui/test_book_batch_dialog.py' = 18
  'tests/storage/test_backup_book_batches.py' = 33
  'tests/audio/test_recording.py' = 31
  'tests/audio/test_recording_controls.py' = 30
  'tests/audio/test_recording_monitor.py' = 21
  'tests/ui/test_recording_dialog.py' = 28
  'tests/storage/test_backup_recording_controls.py' = 20
  'tests/catalog/test_streaming_intake.py' = 16
  'tests/e2e/test_raster_import.py' = 11
  'tests/e2e/test_raster_ocr_native.py' = 3
  'tests/extract/test_raster_pages.py' = 23
  'tests/ingest/test_batch_continuation.py' = 18
  'tests/ingest/test_batch_continuation_cli.py' = 3
  'tests/ui/test_import_center.py' = 42
  'tests/e2e/test_research_search_cli.py' = 15
  'tests/search/test_federated.py' = 100
  'tests/ui/test_federated_search.py' = 114
  'tests/search/test_search_paging.py' = 27
  'tests/ingest/test_scan_root_binding.py' = 4
  'tests/ingest/test_large_source_intake.py' = 6
  'tests/reader/test_export_reconciliation.py' = 28
  'tests/packaging/test_ci_check.py' = 35
  'tests/transcription/test_admission_boundaries.py' = 40
  'tests/transcription/test_service.py' = 28
  'tests/storage/test_desktop_preferences.py' = 22
  'tests/ui/test_desktop_integration.py' = 37
  'tests/ui/test_desktop_settings.py' = 4
  'tests/ui/test_desktop_host_lifecycle.py' = 4
  'tests/speech/test_narration_receipt.py' = 79
  'tests/storage/test_transcription_backup.py' = 143
  'tests/storage/test_local_schedules.py' = 43
  'tests/ui/test_local_schedules.py' = 21
  'tests/storage/test_backup_local_schedules.py' = 23
  'tests/packaging/test_local_schedules_cli.py' = 11
  'tests/speech/test_spelling.py' = 55
  'tests/speech/test_spelling_handlers.py' = 20
  'tests/speech/test_spelling_reader_hook.py' = 9
  'tests/packaging/test_hunspell_build.py' = 31
  'tests/packaging/test_library_scale_benchmark.py' = 30
  'tests/audio/test_envelopes.py' = 64
  'tests/studio/test_envelope_workflow.py' = 17
  'tests/studio/test_clip_operations.py' = 90
  'tests/studio/test_project_history.py' = 29
  'tests/studio/test_clip_history_dialog.py' = 50
  'tests/studio/test_clip_preview_loop.py' = 49
  'tests/studio/test_project_markers.py' = 48
  'tests/studio/test_marker_consumers.py' = 31
  'tests/studio/test_clip_editing.py' = 34
  'tests/studio/test_studio_controller.py' = 18
  'tests/assistant/test_translation_layout.py' = 33
  'tests/ui/test_translation_layout.py' = 23
  'tests/assistant/test_translation_layout_cli.py' = 5
  'tests/assistant/test_translation_partition.py' = 26
  'tests/ui/test_save_all.py' = 24
  'tests/academic_integration/test_word_records.py' = 50
  'tests/academic_integration/test_word_bridge.py' = 20
  'tests/academic_integration/test_word_worker.py' = 17
  'tests/ui/test_word_citations.py' = 17
  'tests/assistant/test_google_oauth.py' = 49
  'tests/ui/test_google_oauth.py' = 15
  'tests/assistant/test_translation_epub_layout.py' = 31
  'tests/ui/test_translation_epub_layout.py' = 6
  'tests/assistant/test_translation_epub_cli.py' = 5
  'tests/studio/test_clip_copy.py' = 17
  'tests/studio/test_clip_copy_dialog.py' = 10
  'tests/extract/test_docx_rich.py' = 55
  'tests/assistant/test_translation_rich_docx.py' = 17
  'tests/ui/test_translation_rich_docx.py' = 5
  'tests/assistant/test_translation_rich_docx_cli.py' = 4
  'tests/extract/test_docx_ordered.py' = 9
  'tests/extract/test_docx_omitted_content.py' = 20
  'tests/extract/test_docx_hidden_styles.py' = 13
  'tests/ingest/test_application_atomic_docx_gate.py' = 1
  'tests/ingest/test_import_replay_identity.py' = 24
  'tests/ingest/test_terminal_replay_registration.py' = 10
  'tests/ingest/test_txt_replay_registration.py' = 7
  'tests/ingest/test_import_publication_point.py' = 2
  'tests/ui/test_native_recording_driver.py' = 10
  'tests/storage/test_scoped_locator_capacity.py' = 4
  'tests/storage/test_locator_integrity.py' = 8
  'tests/storage/test_printed_label_integrity.py' = 37
  'tests/ingest/test_checked_catalog_commit.py' = 10
  'tests/search/test_search_query_work.py' = 17
  'tests/operations/test_broker_scope.py' = 20
  'tests/packaging/test_library_capacity_measurement.py' = 25
  'tests/storage/test_backup_capacity.py' = 28
  'tests/storage/test_backup_capacity_cli.py' = 3
  'tests/ui/test_backup_capacity.py' = 17
  'tests/storage/test_crash_backup.py' = 5
  'tests/studio/test_book_effects.py' = 14
  'tests/studio/test_measured_audio.py' = 36
  'tests/studio/test_measured_pcm.py' = 19
  'tests/studio/test_book_measured_cli.py' = 9
  'tests/ui/test_book_effects.py' = 5
  'tests/ui/test_book_duration_dialog.py' = 17
  'tests/packaging/test_owner_candidate_archives.py' = 22
  'tests/conversion/test_selected_conversion.py' = 39
  'tests/conversion/test_calibre_admission.py' = 49
  'tests/conversion/test_office_admission.py' = 24
  'tests/ui/test_document_conversion.py' = 20
  'tests/extract/test_document_formats_xml.py' = 151
  'tests/transcription/test_native_profiles_fixture.py' = 2
  'tests/ui/test_native_backup_capacity_driver.py' = 11
  'tests/ui/test_native_studio_book_choices.py' = 14
  'tests/ui/test_native_book_publication_driver.py' = 8
  'tests/search/test_paged_candidate_work.py' = 30
  'tests/audio/test_live_metadata_retry.py' = 22
  'tests/ui/test_native_conversion_diagnostics.py' = 30
  'tests/ui/test_native_saved_audio_cleanup.py' = 19
  'tests/audio/test_voice_package_management.py' = 30
  'tests/audio/test_voice_management_state.py' = 25
  'tests/audio/test_voice_reference_management.py' = 30
  'tests/audio/test_voice_protected_management.py' = 15
  'tests/audio/test_voice_publication_lifetime.py' = 15
  'tests/audio/test_voice_ownership.py' = 30
  'tests/integrations/test_dordiai_owner_lease.py' = 28
  'tests/integrations/test_dordiai_managed_authority.py' = 18
  'tests/ui/test_voice_packages.py' = 46
  'tests/ui/test_voice_reference.py' = 32
  'tests/ui/test_ebook_dialog.py' = 38
  'tests/studio/test_publishing_boundaries.py' = 19
  'tests/search/test_window_catalog_projection.py' = 27
  'tests/integrations/test_dordiai_voices.py' = 40
  'tests/integrations/test_dordiai_speech.py' = 10
  'tests/integrations/test_dordiai_settings.py' = 17
  'tests/integrations/test_dordiai_probe_cancellation.py' = 7
  'tests/integrations/test_dordiai_convergence.py' = 10
  'tests/audio/test_bundled_runtime_settings.py' = 8
  'tests/storage/test_v3_state_backup.py' = 42
  'tests/storage/test_library_backup.py' = 9
  'tests/storage/test_state_backup_portability.py' = 27
  'tests/integrations/test_dordiai_reference_backends.py' = 40
  'tests/integrations/test_dordiai_managed_backends.py' = 6
  'tests/citations/test_attestation_cancellation.py' = 15
  'tests/packaging/test_library_scale_profile.py' = 7
  'tests/studio/test_daisy_schema_resolver.py' = 27
  'tests/assistant/test_transcription_provider.py' = 39
  'tests/transcription/test_remote_service.py' = 29
  'tests/ui/test_transcription_remote.py' = 18
  'tests/security/test_update_handoff.py' = 53
  'tests/security/test_update_runtime.py' = 40
  'tests/security/test_update_ui.py' = 15
  'tests/security/test_update_controller_actions.py' = 14
  'tests/audio/test_voice_scope_checks.py' = 42
  'tests/catalog/test_text_screen_work.py' = 26
  'tests/search/test_ascii_mapping_work.py' = 73
  'tests/language_qualification/test_evidence.py' = 54
  'tests/language_qualification/test_evidence_backup.py' = 27
  'tests/language_qualification/test_evidence_ui.py' = 28
}
$formatExpected = @{
  'tests/audio/test_audiobook.py' = 17
  'tests/studio/test_audiobook_publication.py' = 1
  'tests/studio/test_daisy_publishing.py' = 29
  'tests/studio/test_daisy_boundaries.py' = 15
  'tests/studio/test_publishing_boundaries.py' = 19
  'tests/studio/test_publishing_handlers.py' = 12
  'tests/studio/test_publishing_cli.py' = 7
  'tests/storage/test_backup_publication_formats.py' = 8
}

$allSuites = @($suites)
$unique = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
foreach ($suite in $allSuites) {
  if ($suite -cnotmatch '^(tests|bootstrap)/[A-Za-z0-9_./-]+\.py$' -or -not $unique.Add($suite)) { throw "Invalid or duplicate whole-suite selector: $suite" }
}
if ($allSuites.Count -ne 493) { throw 'Exactly 493 reviewed whole source selectors are required.' }
$allCounts = [System.Collections.Generic.Dictionary[string,int]]::new([StringComparer]::Ordinal)
foreach ($mapping in @($operationsCounts, $readerExpected, $completedFeatureCounts, $formatExpected, @{'tests/speech/test_runtime.py'=13})) {
  foreach ($entry in $mapping.GetEnumerator()) {
    if (-not $unique.Contains($entry.Key) -or $entry.Value -lt 1) { throw "Invalid source case contract: $($entry.Key)" }
    if ($allCounts.ContainsKey($entry.Key) -and $allCounts[$entry.Key] -ne $entry.Value) { throw "Conflicting source case contracts: $($entry.Key)" }
    $allCounts[$entry.Key] = $entry.Value
  }
}
# Separate the observed long source suites without changing any child budget.
# Hashtable values retain their List objects instead of enumerating empty lists.
$partitions = @{
  0 = [System.Collections.Generic.List[string]]::new()
  1 = [System.Collections.Generic.List[string]]::new()
  2 = [System.Collections.Generic.List[string]]::new()
  3 = [System.Collections.Generic.List[string]]::new()
}
$anchors = @{
  'tests/research/test_academic_library.py' = 0
  'tests/transcription/test_admission_boundaries.py' = 0
  'tests/studio/test_book_batch.py' = 1
  'tests/ingest/test_large_source_intake.py' = 2
  'tests/search/test_search_paging.py' = 3
}
foreach ($suite in $allSuites) {
  if ($anchors.ContainsKey($suite)) { $partitions[$anchors[$suite]].Add($suite) }
}
foreach ($suite in $allSuites) {
  if ($anchors.ContainsKey($suite)) { continue }
  $target = 0
  for ($candidate = 1; $candidate -lt 4; $candidate++) {
    if ($partitions[$candidate].Count -lt $partitions[$target].Count) { $target = $candidate }
  }
  $partitions[$target].Add($suite)
}
$assignedUnion = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
$shards = [ordered]@{}
for ($index = 0; $index -lt 4; $index++) {
  $selected = @($partitions[$index].ToArray())
  if ($selected.Count -ne @(124,123,123,123)[$index]) { throw 'The reviewed four-shard inventory size differs.' }
  foreach ($suite in $selected) {
    if (-not $unique.Contains($suite) -or -not $assignedUnion.Add($suite)) { throw "Shard overlap or unknown selector: $suite" }
  }
  $shards[[string]$index] = $selected
}
if ($assignedUnion.Count -ne 493 -or -not $assignedUnion.SetEquals($unique)) { throw 'The source shard union is incomplete.' }
function Text-Sha256([string]$Text) {
  $hash = [Security.Cryptography.SHA256]::HashData([Text.Encoding]::UTF8.GetBytes($Text))
  return [Convert]::ToHexString($hash).ToLowerInvariant()
}
$lf = [string][char]10
$countKeys = [string[]]@($allCounts.Keys)
[Array]::Sort($countKeys, [StringComparer]::Ordinal)
$countLines = @($countKeys | ForEach-Object { $_ + '=' + $allCounts[$_] })
$inventory = [ordered]@{
  schema = 'carr-integrated-source-inventory/1'
  complete = $allSuites
  complete_count = 493
  shards = $shards
  inventory_sha256 = Text-Sha256 (($allSuites -join $lf) + $lf)
  expected_counts = $allCounts
  count_contract_sha256 = Text-Sha256 (($countLines -join $lf) + $lf)
}
if ($InventoryOnly) {
  $inventory | ConvertTo-Json -Depth 12 -Compress
  return
}
if ($ExpectedHead -cnotmatch '^[0-9a-f]{40}$' -or -not $env:CARR_PYTHON) { throw 'Exact event HEAD and the isolated source interpreter are required.' }
$head = (& git rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or $head -cne $ExpectedHead) { throw 'The checked-out source differs from the selected event HEAD.' }
$tree = (& git rev-parse 'HEAD^{tree}').Trim()
if ($LASTEXITCODE -ne 0 -or $tree -cnotmatch '^[0-9a-f]{40}$') { throw 'The exact source tree is unavailable.' }
$helperSha256 = (Get-FileHash -LiteralPath $PSCommandPath -Algorithm SHA256).Hash.ToLowerInvariant()
$suites = @($partitions[$Shard].ToArray())
$selectionSha256 = Text-Sha256 (($suites -join $lf) + $lf)
New-Item -ItemType Directory -Force 'ci-results' | Out-Null
$inventory | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath 'ci-results/source-inventory.json' -Encoding utf8

$sourceProof = @'
from hashlib import sha1, sha256
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory

phase, expected_head = sys.argv[1:]
if phase not in ('before', 'after'):
    raise ValueError('Use a bounded source-proof phase.')
root = Path(subprocess.check_output(['git', 'rev-parse', '--show-toplevel'], text=True).strip())
head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
tree = subprocess.check_output(['git', 'rev-parse', 'HEAD^{tree}'], cwd=root, text=True).strip()
if head != expected_head:
    raise ValueError('The source HEAD changed.')
rows = subprocess.check_output([
    'git', 'ls-tree', '-rz', 'HEAD', '--', 'carr-library',
    '.github/workflows/carr-windows.yml',
    '.github/workflows/carr-integrated-source-shard.yml',
], cwd=root).split(b'\0')
def ordinary(info, directory=False):
    okay = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if (not okay or stat.S_ISLNK(info.st_mode)
            or getattr(info, 'st_file_attributes', 0) & 0x400
            or (not directory and info.st_nlink != 1)):
        raise ValueError('Source proof requires ordinary unlinked local files.')
def stamp(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)
def descriptor_path_stamp(info, path):
    value = stamp(info)
    if os.name == 'nt' and path.suffix.casefold() in ('.exe', '.bat', '.cmd', '.com'):
        # CPython 3.11 win32_xstat_impl adds these filename-derived bits.
        # Its _Py_fstat_noraise sees the same handle metadata without a filename.
        value = (*value[:2], value[2] | 0o111, *value[3:])
    return value
def descriptor_controls():
    controls = 0
    with TemporaryDirectory(prefix='carr-source-descriptor-control-') as temporary:
        root = Path(temporary)
        for suffix in ('.txt', '.py', '.cmd', '.BAT', '.exe', '.com'):
            path = root / ('controlled' + suffix)
            path.write_bytes(b'fixed source proof fixture\n')
            before = path.lstat()
            ordinary(before)
            with path.open('rb') as stream:
                opened = os.fstat(stream.fileno())
                ordinary(opened)
                if descriptor_path_stamp(opened, path) != stamp(before):
                    raise ValueError('Real source descriptor control failed: ' + suffix)
                stream.read()
                if stamp(os.fstat(stream.fileno())) != stamp(opened):
                    raise ValueError('Real source descriptor control changed during reading.')
            if stamp(path.lstat()) != stamp(before):
                raise ValueError('Real source descriptor control path changed.')
            controls += 1
        path = root / 'identity.cmd'
        path.write_bytes(b'same bytes\n')
        original = path.lstat()
        path.rename(root / 'retired.cmd')
        path.write_bytes(b'same bytes\n')
        with path.open('rb') as stream:
            if descriptor_path_stamp(os.fstat(stream.fileno()), path) == stamp(original):
                raise ValueError('Source proof accepted an actual same-byte file replacement.')
        controls += 1
        original = path.lstat()
        with path.open('ab') as stream:
            stream.write(b'changed\n')
        with path.open('rb') as stream:
            if descriptor_path_stamp(os.fstat(stream.fileno()), path) == stamp(original):
                raise ValueError('Source proof accepted actual changed file bytes/size.')
        controls += 1
    if controls != 8:
        raise ValueError('The real source descriptor controls are incomplete.')
    print(json.dumps(dict(status='PASS', source_descriptor_controls=controls), sort_keys=True))
if phase == 'before':
    descriptor_controls()
files, identities = {}, {}
for row in rows:
    if not row:
        continue
    description, raw_name = row.split(b'\t', 1)
    mode, kind, blob = description.decode('ascii').split()
    name = raw_name.decode('utf-8')
    if kind != 'blob' or mode not in ('100644', '100755') or name in files:
        raise ValueError('The tracked source inventory contains an unsupported entry.')
    path = root.joinpath(*name.split('/'))
    if path == root or not path.is_relative_to(root):
        raise ValueError('The tracked source escaped its root.')
    parent = path.parent
    while True:
        ordinary(parent.lstat(), directory=True)
        if parent == root:
            break
        parent = parent.parent
    before = path.lstat()
    ordinary(before)
    git_hash = sha1(b'blob ' + str(before.st_size).encode('ascii') + b'\0')
    content_hash = sha256()
    count = 0
    with path.open('rb') as stream:
        opened = os.fstat(stream.fileno())
        ordinary(opened)
        if descriptor_path_stamp(opened, path) != stamp(before):
            raise ValueError('A source file changed while opening: ' + name
                             + '; path=' + repr(stamp(before))
                             + '; descriptor=' + repr(stamp(opened)))
        while block := stream.read(1024 * 1024):
            count += len(block)
            if count > before.st_size:
                raise ValueError('A source file grew while hashing.')
            git_hash.update(block)
            content_hash.update(block)
        if stamp(os.fstat(stream.fileno())) != stamp(opened):
            raise ValueError('A source descriptor changed while hashing: ' + name)
    if stamp(path.lstat()) != stamp(before) or count != before.st_size:
        raise ValueError('A source path changed while hashing.')
    if git_hash.hexdigest() != blob:
        raise ValueError('Checked-out source bytes differ from the selected Git object: ' + name)
    files[name] = {'blob': blob, 'sha256': content_hash.hexdigest(), 'bytes': count, 'mode': mode}
    identities[name] = list(stamp(before))
    if len(files) > 50000:
        raise ValueError('The source inventory exceeded its finite file allowance.')
if not files:
    raise ValueError('The tracked source inventory is empty.')
source_sha256 = sha256(json.dumps(files, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
report = dict(schema='carr-integrated-source-proof/1', status='PASS', head=head, tree=tree,
              files=files, identities=identities, source_sha256=source_sha256)
before_path = root / 'carr-library/ci-results/source-before.json'
if phase == 'after' and json.loads(before_path.read_text(encoding='utf-8')) != report:
    raise ValueError('The tracked source or file identities changed during source acceptance.')
target = root / ('carr-library/ci-results/source-' + phase + '.json')
raw = (json.dumps(report, sort_keys=True, separators=(',', ':')) + '\n').encode()
if len(raw) > 8 * 1024 * 1024:
    raise ValueError('The bounded source-proof report is too large.')
with target.open('xb') as stream:
    stream.write(raw)
print(json.dumps(dict(status='PASS', phase=phase, head=head, tree=tree,
                      files=len(files), source_sha256=source_sha256), sort_keys=True))
'@
& $env:CARR_PYTHON -I -B -c $sourceProof before $ExpectedHead
if ($LASTEXITCODE -ne 0) { throw 'The required before-test source proof failed.' }
$sourceBeforeSha256 = (Get-FileHash -LiteralPath 'ci-results/source-before.json' -Algorithm SHA256).Hash.ToLowerInvariant()

$primaryFailure = $null
$previousPath = $env:PATH
$previousOcr = $env:CARR_OCR_DIR
$previousTessdata = $env:TESSDATA_PREFIX
try {
  if (-not $env:CARR_CI_AUDIO_DIR -or -not $env:CARR_CI_OCR_DIR) { throw 'Verified audio and OCR prerequisites are required for source acceptance.' }
  $audioBin = Join-Path $env:CARR_CI_AUDIO_DIR 'bin'
  foreach ($tool in @('ffmpeg.exe', 'ffprobe.exe')) {
    if (-not (Test-Path -LiteralPath (Join-Path $audioBin $tool) -PathType Leaf)) { throw "Verified audio executable is missing: $tool" }
  }
  if (-not (Test-Path -LiteralPath (Join-Path $env:CARR_CI_OCR_DIR 'tesseract.exe') -PathType Leaf)) { throw 'Verified OCR executable is missing.' }
  $env:PATH = "$audioBin;$env:CARR_CI_OCR_DIR;$previousPath"
  $env:CARR_OCR_DIR = $env:CARR_CI_OCR_DIR
  $env:TESSDATA_PREFIX = Join-Path $env:CARR_CI_OCR_DIR 'tessdata'
  # All newly integrated reader/Find and trust modules remain whole-module strict gates.
  # The actual Windows run completed this suite alone in 208 seconds;
  # another run reached its unchanged 600-second bound alongside other suites.
  $academicSuite = 'tests/research/test_academic_library.py'
  $parallelSuites = @($suites | Where-Object { $_ -cne $academicSuite })
  $academicExit = 0
  if ($suites -ccontains $academicSuite) {
    if ($parallelSuites.Count + 1 -ne $suites.Count) { throw 'Required academic suite must occur exactly once.' }
    & $env:CARR_PYTHON scripts/ci_check.py --jobs 1 $academicSuite 2>&1 | Tee-Object 'ci-results/integrated-tests.txt'
    $academicExit = $LASTEXITCODE
  } elseif ($parallelSuites.Count -ne $suites.Count) {
    throw 'A different shard must own the complete academic suite.'
  }
  $parallelArguments = @('--jobs', '3')
  if ($parallelSuites -ccontains 'tests/transcription/test_admission_boundaries.py') {
    $parallelArguments += @('--suite-timeout', 'tests/transcription/test_admission_boundaries.py=900')
  }
  & $env:CARR_PYTHON scripts/ci_check.py @parallelArguments @parallelSuites 2>&1 | Tee-Object -Append 'ci-results/integrated-tests.txt'
  $parallelExit = $LASTEXITCODE
  if ($academicExit -ne 0 -or $parallelExit -ne 0) { throw 'Required whole source suites failed.' }
  foreach ($entry in $operationsCounts.GetEnumerator()) {
    if ($suites -cnotcontains $entry.Key) { continue }
    $pattern = '^PASS: ' + [regex]::Escape($entry.Key) + ': junit_passed=' + $entry.Value + ', failed=0, skipped=0, pytest_exit=0$'
    if (@(Select-String -LiteralPath 'ci-results/integrated-tests.txt' -Pattern $pattern).Count -ne 1) { throw ('Required operations case inventory differs: ' + $entry.Key) }
  }
  if ($suites -ccontains 'tests/speech/test_runtime.py') {
    $speechVerdict = @(Select-String -LiteralPath 'ci-results/integrated-tests.txt' -Pattern '^PASS: tests/speech/test_runtime\.py: junit_passed=13, failed=0, skipped=0, pytest_exit=0$')
    if ($speechVerdict.Count -ne 1) { throw 'All 12 shared speech cases and the actual Windows SAPI case are required.' }
  }
  foreach ($entry in $readerExpected.GetEnumerator()) {
    if ($suites -cnotcontains $entry.Key) { continue }
    $pattern = '^PASS: ' + [regex]::Escape($entry.Key) + ': junit_passed=' + $entry.Value + ', failed=0, skipped=0, pytest_exit=0$'
    $verdict = @(Select-String -LiteralPath 'ci-results/integrated-tests.txt' -Pattern $pattern)
    if ($verdict.Count -ne 1) { throw ("Required reader coverage differs: " + $entry.Key) }
  }
  foreach ($entry in $completedFeatureCounts.GetEnumerator()) {
    if ($suites -cnotcontains $entry.Key) { continue }
    $pattern = '^PASS: ' + [regex]::Escape($entry.Key) + ': junit_passed=' + $entry.Value + ', failed=0, skipped=0, pytest_exit=0$'
    if (@(Select-String -LiteralPath 'ci-results/integrated-tests.txt' -Pattern $pattern).Count -ne 1) { throw ('Required completed-feature case inventory differs: ' + $entry.Key) }
  }
  foreach ($entry in $formatExpected.GetEnumerator()) {
    if ($suites -cnotcontains $entry.Key) { continue }
    $pattern = '^PASS: ' + [regex]::Escape($entry.Key) + ': junit_passed=' + $entry.Value + ', failed=0, skipped=0, pytest_exit=0$'
    if (@(Select-String -LiteralPath 'ci-results/integrated-tests.txt' -Pattern $pattern).Count -ne 1) { throw ("Required publication-format coverage differs: " + $entry.Key) }
  }
} catch {
  $primaryFailure = $_
} finally {
  $env:PATH = $previousPath
  $env:CARR_OCR_DIR = $previousOcr
  $env:TESSDATA_PREFIX = $previousTessdata
  try {
    & $env:CARR_PYTHON -I -B -c $sourceProof after $ExpectedHead
    if ($LASTEXITCODE -ne 0) { throw 'The required after-test source proof failed.' }
  } catch {
    if ($null -eq $primaryFailure) { $primaryFailure = $_ }
    else { Write-Output ('Additional source-proof failure: ' + $_.Exception.Message) }
  }
}
if ($null -ne $primaryFailure) { throw $primaryFailure }



if ((Get-FileHash -LiteralPath 'ci-results/source-before.json' -Algorithm SHA256).Hash.ToLowerInvariant() -cne $sourceBeforeSha256) { throw 'The captured before-test source proof changed.' }
if ((Get-FileHash -LiteralPath $PSCommandPath -Algorithm SHA256).Hash.ToLowerInvariant() -cne $helperSha256) { throw 'The source shard helper changed during execution.' }
$verdicts = [System.Collections.Generic.Dictionary[string,int]]::new([StringComparer]::Ordinal)
foreach ($line in [IO.File]::ReadAllLines((Join-Path (Get-Location) 'ci-results/integrated-tests.txt'))) {
  if ($line -cmatch '^(PASS|FAIL): ((?:tests|bootstrap)/[A-Za-z0-9_./-]+\.py): junit_passed=([0-9]+), failed=([0-9]+), skipped=([0-9]+), pytest_exit=(-?[0-9]+)$') {
    $path = $Matches[2]
    if ($Matches[1] -cne 'PASS' -or [int]$Matches[3] -lt 1 -or [int]$Matches[4] -ne 0 -or [int]$Matches[5] -ne 0 -or [int]$Matches[6] -ne 0 -or $suites -cnotcontains $path -or $verdicts.ContainsKey($path)) { throw "Invalid or duplicate strict source verdict: $path" }
    $verdicts.Add($path, [int]$Matches[3])
  } elseif ($line -cmatch '^(PASS|FAIL): (tests|bootstrap)/') {
    throw 'A source verdict did not satisfy the complete strict result grammar.'
  }
}
if ($verdicts.Count -ne $suites.Count) { throw 'One or more assigned whole suites lack a complete strict verdict.' }
foreach ($suite in $suites) {
  if (-not $verdicts.ContainsKey($suite)) { throw "Missing required source verdict: $suite" }
  if ($allCounts.ContainsKey($suite) -and $verdicts[$suite] -ne $allCounts[$suite]) { throw "The audited whole-suite count differs: $suite" }
}
$after = Get-Content -LiteralPath 'ci-results/source-after.json' -Raw | ConvertFrom-Json
if ($after.status -cne 'PASS' -or $after.head -cne $head -or $after.tree -cne $tree -or $after.source_sha256 -cnotmatch '^[0-9a-f]{64}$') { throw 'The final source proof is incomplete.' }
$receipt = [ordered]@{
  schema = 'carr-required-source-shard/1'
  status = 'PASS'
  head = $head
  tree = $tree
  shard = $Shard
  complete_count = 493
  assigned = $suites
  assigned_count = $suites.Count
  inventory_sha256 = $inventory.inventory_sha256
  selection_sha256 = $selectionSha256
  count_contract_sha256 = $inventory.count_contract_sha256
  helper_sha256 = $helperSha256
  source_sha256 = $after.source_sha256
  strict_verdict_count = $verdicts.Count
  zero_failure_skip_policy = 'PASS'
  log_sha256 = (Get-FileHash -LiteralPath 'ci-results/integrated-tests.txt' -Algorithm SHA256).Hash.ToLowerInvariant()
  jobs = 3
  academic_jobs = 1
  default_child_seconds = 600
  reviewed_asr_seconds = 900
}
$json = $receipt | ConvertTo-Json -Depth 12 -Compress
if ([Text.Encoding]::UTF8.GetByteCount($json) -gt 32768) { throw 'The source receipt exceeded its finite output allowance.' }
[IO.File]::WriteAllText((Join-Path (Get-Location) 'ci-results/source-shard.json'), $json + $lf, [Text.UTF8Encoding]::new($false))
if (-not $env:GITHUB_OUTPUT) { throw 'The required caller receipt output is unavailable.' }
('receipt=' + $json) | Out-File -LiteralPath $env:GITHUB_OUTPUT -Encoding utf8 -Append
Write-Output ('PASS: required integrated source shard ' + $Shard + ', whole_suites=' + $suites.Count + ', head=' + $head)
