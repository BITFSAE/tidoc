/* 与 Python 后端的通信封装。所有后端方法通过 window.pywebview.api 暴露，
   统一返回 {ok, data|error}。这里做等待桥就绪 + 解包 + 报错。 */

const Api = (() => {
  let ready = null;

  function waitReady() {
    if (ready) return ready;
    // 桥就绪判定：api 对象存在且已挂上方法。pywebview 会先注入空的 api={}，
    // 待 _createApi 执行后才填充函数，故不能只判断 api 存在（空对象也为真）。
    const bridgeReady = () =>
      window.pywebview && window.pywebview.api &&
      typeof window.pywebview.api.list_profiles === 'function';
    ready = new Promise((resolve) => {
      if (bridgeReady()) return resolve();
      window.addEventListener('pywebviewready', () => {
        // ready 事件后方法即已注入，但保险起见再轮询确认
        if (bridgeReady()) return resolve();
        const t0 = setInterval(() => {
          if (bridgeReady()) { clearInterval(t0); resolve(); }
        }, 30);
      }, { once: true });
      // 兜底轮询（某些平台事件时机不稳）
      const t = setInterval(() => {
        if (bridgeReady()) {
          clearInterval(t);
          resolve();
        }
      }, 50);
    });
    return ready;
  }

  async function call(method, ...args) {
    await waitReady();
    const fn = window.pywebview.api[method];
    if (!fn) throw new Error(`后端方法不存在：${method}`);
    const res = await fn(...args);
    if (res && res.ok === false) { const error = new Error(res.error || '未知错误'); error.diagnostics = res.diagnostics || []; error.code = res.code; throw error; }
    return res && 'data' in res ? res.data : res;
  }

  return {
    ready: waitReady,
    listSchemes: (includeDisabled=false) => call('list_schemes',includeDisabled),
    schemeDetails: (id=null) => call('scheme_details',id),
    schemeRevisionHistory: (id) => call('scheme_revision_history',id),
    adapterSetupState: () => call('adapter_setup_state'),
    completeAdapterSetup: (id=null,preferences=null) => call('complete_adapter_setup',id,preferences),
    inspectAdapter: (path,operation=null) => call('inspect_adapter',path,operation),
    chooseAdapterFile: (operation=null) => call('choose_adapter_file',operation),
    installAdapter: (id,options,operation=null) => call('install_adapter',id,options||{},operation),
    adapterOperationStatus: (id) => call('adapter_operation_status',id),
    cancelAdapterOperation: (id) => call('cancel_adapter_operation',id),
    setDefaultScheme: (id) => call('set_default_scheme',id),
    copyScheme: (id,name) => call('copy_scheme',id,name),
    disableScheme: (id) => call('disable_scheme',id),
    enableScheme: (id) => call('enable_scheme',id),
    updateSchemeSettings: (id,revision,values,clear=null) => call('update_scheme_settings',id,revision,values,clear),
    restoreSchemeDefaults: (id,revision,keys=null) => call('restore_scheme_defaults',id,revision,keys),
    rollbackScheme: (id,revision,expected) => call('rollback_scheme',id,revision,expected),
    exportAdapter: (id,options) => call('export_adapter',id,options||{}),
    getFormDescription: (scope,id,scheme=null,revision=null) => call('get_form_description',scope,id,scheme,revision),
    previewFormDescription: (scope,id,values,scheme=null,revision=null) => call('preview_form_description',scope,id,values,scheme,revision),
    updateBatchOutputSettings: (id,values,updatedAt=null) => call('update_batch_output_settings',id,values,updatedAt),
    saveExtensionValues: (scope,id,values,scheme=null,version=null,revision=null) => call('save_extension_values',scope,id,values,scheme,version,revision),
    batchSaveExtensionValues: (ids,field,value) => call('batch_save_extension_values',ids,field,value),
    listPayees: () => call('list_payees'),
    savePayee: (id,values) => call('save_payee',id,values),
    deletePayee: (id) => call('delete_payee',id),
    setPayeeMapping: (scheme,profile,payee) => call('set_payee_mapping',scheme,profile,payee),
    setSchemePayee: (scheme,payee) => call('set_scheme_payee',scheme,payee),
    setBatchPayee: (batch,scheme,payee) => call('set_batch_payee',batch,scheme,payee),
    previewRebind: (ids,scheme,revision=null,mappings=null) => call('preview_rebind',ids,scheme,revision,mappings),
    applyRebind: (id,operation=null) => call('apply_rebind',id,operation),
    previewExport: (ids,outputs,options) => call('preview_export',ids,outputs,options||{}),
    runExport: (id,dir=null) => call('run_export',id,dir),
    cancelExport: (id) => call('cancel_export',id),
    listExportJobs: () => call('list_export_jobs'),
    getExportJob: (id) => call('get_export_job',id),
    getExportProgress: (id) => call('get_export_progress',id),
    regenerateExport: (id) => call('regenerate_export',id),
    deleteExportJob: (id,files=false) => call('delete_export_job',id,files),
    reclassifyAttachment: (id,role) => call('reclassify_attachment',id,role),

    listProfiles: () => call('list_profiles'),
    createProfile: (name, reviewer, isDefault, opt) => call('create_profile', name, reviewer, isDefault, opt || {}),
    updateProfile: (id, fields) => call('update_profile', id, fields || {}),
    setDefaultProfile: (id) => call('set_default_profile', id),
    deleteProfile: (id) => call('delete_profile', id),
    appPreference: (key, defaultValue) => call('app_preference', key, defaultValue || ''),
    setAppPreference: (key, value) => call('set_app_preference', key, value || ''),
    materialRequirements: () => call('material_requirements'),
    setMaterialRequirements: (requirements) => call('set_material_requirements', requirements || {}),
    titleProfiles: () => call('title_profiles'),
    setTitleProfiles: (profiles) => call('set_title_profiles', profiles || []),
    takeLaunchFile: () => call('take_launch_file'),
    invoiceVerificationPreferences: () => call('invoice_verification_preferences'),
    setInvoiceVerificationPreferences: (options) => call('set_invoice_verification_preferences', options || {}),
    appInfo: () => call('app_info'),
    startupUpdateState: () => call('startup_update_state'),
    markFrontendReady: () => call('mark_frontend_ready'),

    parseFiles: (xml, pdf,scheme=null) => call('parse_files', xml, pdf,scheme),
    reparseEntries: (ids) => call('reparse_entries', ids || []),
    recognitionPreview: (ids) => call('recognition_preview', ids || []),
    rerecognizeMaterials: (ids, kinds) => call('rerecognize_materials', ids || [], kinds || []),
    createEntry: (args) => call('create_entry',
      args.profileId, args.title || '', args.xmlPath || null, args.pdfPath || null,
      args.paymentPaths || [], args.inspectionPath || null, args.status || 'draft',
      args.physicalPaths || [],args.schemeId||null,args.batchId||null),

    listEntries: (filters) => call('list_entries', filters || {}),
    listTitles: () => call('list_titles'),
    getEntry: (id) => call('get_entry', id),
    updateField: (id, field, value, pid) => call('update_field', id, field, value, pid || ''),
    setRecognizedPaidAmount: (id, value) => call('set_recognized_paid_amount', id, value),
    correctLocked: (id, field, value, pid) => call('correct_locked_field', id, field, value, pid || ''),
    updateEntryProfile: (id, profileId, operatorProfileId) => call('update_entry_profile', id, profileId, operatorProfileId || ''),
    updateEntryProfiles: (ids, profileId, operatorProfileId) => call('update_entry_profiles', ids || [], profileId, operatorProfileId || ''),
    setStatus: (id, status) => call('set_status', id, status),
    setMeta: (id, category, tags) => call('set_meta', id, category, tags),
    deleteEntry: (id) => call('delete_entry', id),
    deleteEntries: (ids) => call('delete_entries', ids),

    addTag: (ids, tag) => call('add_tag', ids, tag),
    removeTag: (ids, tag) => call('remove_tag', ids, tag),
    listTags: () => call('list_tags'),
    renameTag: (oldTag, newTag) => call('rename_tag', oldTag, newTag),
    deleteTag: (tag) => call('delete_tag', tag),

    listBatches: (includeArchived) => call('list_batches', !!includeArchived),
    getBatch: (id) => call('get_batch', id),
    createBatch: (name, note, entryIds) => call('create_batch', name, note || '', entryIds || []),
    updateBatch: (id, fields) => call('update_batch', id, fields || {}),
    archiveBatch: (id, archived) => call('archive_batch', id, archived !== false),
    deleteBatch: (id, deleteEntries) => call('delete_batch', id, !!deleteEntries),
    addEntriesToBatch: (id, entryIds) => call('add_entries_to_batch', id, entryIds || []),
    removeEntriesFromBatch: (id, entryIds) => call('remove_entries_from_batch', id, entryIds || []),
    moveEntriesBetweenBatches: (sourceId, targetId, entryIds) => call('move_entries_between_batches', sourceId, targetId, entryIds || []),
    setEntryBatch: (entryId, batchId) => call('set_entry_batch', entryId, batchId || ''),
    setEntriesBatch: (entryIds, batchId) => call('set_entries_batch', entryIds || [], batchId || ''),
    setBatchEntryNote: (id, entryId, note) => call('set_batch_entry_note', id, entryId, note || ''),
    batchesOfEntry: (entryId) => call('batches_of_entry', entryId),

    addItem: (entryId, fields) => call('add_item', entryId, fields || {}),
    updateItem: (itemId, fields) => call('update_item', itemId, fields || {}),
    deleteItem: (itemId) => call('delete_item', itemId),

    addAttachment: (id, path, type, note, options) => call('add_attachment', id, path, type, note || '', options || null),
    deleteAttachment: (id) => call('delete_attachment', id),
    setAttachmentNote: (id, note) => call('set_attachment_note', id, note),
    updateAttachment: (id, fields) => call('update_attachment', id, fields || {}),
    openAttachment: (id) => call('open_attachment', id),
    revealAttachment: (id) => call('reveal_attachment', id),
    invoiceVerificationInfo: (id) => call('invoice_verification_info', id),
    startInvoiceVerification: (id, fields) => call('start_invoice_verification', id, fields || {}),
    invoiceVerificationStatus: (sessionId) => call('invoice_verification_status', sessionId),
    closeInvoiceVerification: (sessionId) => call('close_invoice_verification', sessionId),

    printComponentStatus: () => call('print_component_status'),
    buildPrints: (ids, options, name) => call('build_prints', ids, options || null, name || null),
    ocrComponentStatus: () => call('ocr_component_status'),
    saveOcrCredentials: (keyId, keySecret) => call('save_ocr_credentials', keyId, keySecret),
    clearOcrCredentials: () => call('clear_ocr_credentials'),
    ocrPreview: (ids) => call('ocr_preview', ids || []),
    runOcrRecognition: (ids, options) => call('run_ocr_recognition', ids || [], options || null),
    getOcrResult: (id) => call('get_ocr_result', id),
    applyOcrField: (id, field) => call('apply_ocr_field', id, field),
    applyOcrItems: (id) => call('apply_ocr_items', id),
    checkUpdates: () => call('check_updates'),
    autoCheckUpdates: () => call('auto_check_updates'),
    coreUpdateStatus: () => call('core_update_status'),
    startCoreUpdateDownload: () => call('start_core_update_download'),
    installCoreUpdate: () => call('install_core_update'),
    downloadCoreUpdate: () => call('download_core_update'),
    openDownloadedCoreUpdate: () => call('open_downloaded_core_update'),
    installPrintComponent: () => call('install_print_component'),
    installOcrComponent: () => call('install_ocr_component'),

    buildSummary: (ids) => call('build_summary', ids),
    previewBindleTransfer: (ids) => call('preview_bindle_transfer',ids),
    exportBindle: (ids, name,options=null) => call('export_bindle', ids, name,options),
    exportOverviewExcel: (ids, name) => call('export_overview_excel', ids, name),
    exportAttachmentArchive: (ids, name) => call('export_attachment_archive', ids, name),
    inspectBindle: (path,scheme=null) => call('inspect_bindle', path,scheme),
    importBindle: (path, pid, allowTampered, options) => call('import_bindle', path, pid, !!allowTampered, options || null),

    pickFiles: (multiple, fileTypes) => call('pick_files', multiple !== false, fileTypes || null),
    pickFolder: () => call('pick_folder'),
    scanFolder: (folder) => call('scan_folder', folder),
    scanFiles: (paths) => call('scan_files', paths || []),
    classifyMaterialFiles: (paths) => call('classify_material_files', paths || []),
    suggestMaterialBindings: (infos, candidateEntryIds) => call('suggest_material_bindings', infos || [], candidateEntryIds || []),
    saveDroppedFiles: (files) => call('save_dropped_files', files || []),
    cleanupDroppedFiles: (paths) => call('cleanup_dropped_files', paths || []),
    batchCreateEntries: (profileId, groups, title,scheme=null,batch=null) => call('batch_create_entries', profileId, groups, title || '',scheme,batch),
    dataRoot: () => call('data_root_path'),
    chooseAndMigrateDataRoot: () => call('choose_and_migrate_data_root'),
    resetDataRootToDefault: () => call('reset_data_root_to_default'),
    storageMaintenanceStatus: () => call('storage_maintenance_status'),
    cleanupOldBackups: () => call('cleanup_old_backups'),
    cleanupAppCache: () => call('cleanup_app_cache'),
    cleanupOldBackups: () => call('cleanup_old_backups'),
    openPath: (path) => call('open_path', path),
    openExternalUrl: (url) => call('open_external_url', url),
  };
})();
