"""Scheme and export-plan bridge methods."""
from __future__ import annotations
from .api_shared import _guard


class SchemeApi:
    @_guard
    def list_schemes(self, include_disabled=False):
        return self.adapters.list_schemes(include_disabled)

    @_guard
    def scheme_details(self, scheme_id=None):
        scheme = self.adapters.get_scheme(scheme_id)
        # 方案包自身给出的设置值（不含本地覆盖）：界面据此标出已自定义的项，并只保存真正改动的项。
        baseline = self.adapters.package_baseline(scheme)
        scheme["settings_baseline"] = baseline["effective_settings"]
        # 抬头与材料要求同理：界面据此标出已自定义的项，并提供「恢复默认」。
        scheme["titles_baseline"] = baseline["scheme"].get("titles", [])
        scheme["requirements_baseline"] = self.adapters.requirements_of(baseline)
        scheme["requirements"] = self.adapters.requirements_of(scheme["definition"])
        return scheme

    @_guard
    def adapter_setup_state(self):
        return self.adapters.setup_state()

    @_guard
    def complete_adapter_setup(self, scheme_id=None, legacy_preferences=None):
        result = self.adapters.complete_adapter_setup(scheme_id,legacy_preferences)
        self._sync_entry_statuses()
        return result

    @_guard
    def inspect_adapter(self,path,operation_id=None):
        return self.adapters.inspect_adapter(path,operation_id=operation_id)

    @_guard
    def choose_adapter_file(self,operation_id=None):
        paths = self.pick_files(False,["报账方案 (*.tidoc-preset)"])
        if not paths.get("ok"):
            return paths
        selected = paths.get("paths") or []
        return self.adapters.inspect_adapter(selected[0],operation_id=operation_id) if selected else None

    @_guard
    def install_adapter(self,preview_id,options=None,operation_id=None):
        return self.adapters.install_adapter(preview_id,options,operation_id=operation_id)

    def adapter_operation_status(self,operation_id):
        # Progress must remain readable while the mutation holds the API lock.
        try:
            return {"ok":True,"data":self.adapters.operation_status(operation_id)}
        except Exception as exc:
            return {"ok":False,"error":str(exc)}

    def cancel_adapter_operation(self,operation_id):
        try:
            return {"ok":True,"data":self.adapters.cancel_operation(operation_id)}
        except Exception as exc:
            return {"ok":False,"error":str(exc)}

    @_guard
    def set_default_scheme(self,scheme_id):
        return self.adapters.set_default_scheme(scheme_id)

    @_guard
    def enable_scheme(self,scheme_id):
        return self.adapters.enable_scheme(scheme_id)

    @_guard
    def copy_scheme(self,scheme_id,name):
        return self.adapters.copy_scheme(scheme_id,name)

    @_guard
    def disable_scheme(self,scheme_id):
        return self.adapters.disable_scheme(scheme_id)

    @_guard
    def update_scheme(self,scheme_id,expected_revision,changes=None):
        """一次保存方案的设置、抬头和材料要求，只生成一个新修订。"""
        changes=changes or {}
        unknown=set(changes)-{"settings","clear","titles","material_requirements"}
        if unknown:
            raise ValueError("方案更改包含未知项："+"、".join(sorted(unknown)))
        return self.adapters.update_scheme(scheme_id,expected_revision,
            settings=changes.get("settings"),clear=changes.get("clear"),
            titles=changes.get("titles"),material_requirements=changes.get("material_requirements"))

    @_guard
    def update_scheme_settings(self,scheme_id,expected_revision,values,clear=None):
        return self.adapters.update_scheme_settings(scheme_id,expected_revision,values,clear)

    @_guard
    def restore_scheme_defaults(self,scheme_id,expected_revision,keys=None):
        return self.adapters.restore_setting_defaults(scheme_id,expected_revision,keys)

    @_guard
    def scheme_revision_history(self,scheme_id):
        return self.adapters.revision_history(scheme_id)

    @_guard
    def rollback_scheme(self,scheme_id,revision_id,expected_revision=None):
        return self.adapters.rollback_scheme(scheme_id,revision_id,expected_revision)

    @_guard
    def export_adapter(self,scheme_id,options=None):
        return self.adapters.export_adapter(scheme_id,options or {})

    @_guard
    def preview_rebind(self,entry_ids,scheme_id,revision_id=None,mappings=None):
        return self.adapters.preview_rebind(entry_ids,scheme_id,revision_id,mappings)

    @_guard
    def apply_rebind(self,preview_id,operation_id=None):
        return self.adapters.apply_rebind(preview_id,operation_id=operation_id)

    @_guard
    def get_form_description(self,scope,owner_id,scheme_id=None,revision_id=None):
        return self.adapters.get_form_description(scope,owner_id,scheme_id,revision_id)

    @_guard
    def preview_form_description(self,scope,owner_id,values,scheme_id=None,revision_id=None):
        return self.adapters.preview_form_description(scope,owner_id,values,scheme_id,revision_id)

    @_guard
    def update_batch_output_settings(self,batch_id,values,expected_updated_at=None):
        return self.adapters.update_batch_output_settings(batch_id,values,expected_updated_at)

    @_guard
    def save_extension_values(self,scope,owner_id,values,scheme_id=None,expected_version=None,revision_id=None):
        return self.adapters.save_extension_values(scope,owner_id,values,scheme_id,expected_version,revision_id)

    @_guard
    def batch_save_extension_values(self,entry_ids,field_id,value):
        forms = [self.adapters.get_form_description("entry",eid) for eid in dict.fromkeys(entry_ids or [])]
        definitions = [next((f for f in form["fields"] if f["id"]==field_id),None) for form in forms]
        if not definitions or any(f is None for f in definitions) or any((f["type"],f.get("options"),form["package_id"]) != (definitions[0]["type"],definitions[0].get("options"),forms[0]["package_id"]) for f,form in zip(definitions,forms)):
            raise ValueError("所选条目的字段定义不兼容，请按方案分别填写。")
        with self.db.transaction():
            for form in forms:
                self.adapters.extensions.save_values("entry",form["owner_id"],form["scheme_id"],form["revision_id"],{field_id:value},commit=False)
                self.entries.recompute_status(form["owner_id"],commit=False)
        return {"changed":len(forms)}

    @_guard
    def list_payees(self):
        return self.adapters.list_payees()

    @_guard
    def save_payee(self,payee_id=None,values=None):
        return self.adapters.save_payee(payee_id,values)

    @_guard
    def delete_payee(self,payee_id):
        return self.adapters.payees.delete(payee_id)

    @_guard
    def set_payee_mapping(self,scheme_id,profile_id,payee_id):
        return self.adapters.set_payee_mapping(scheme_id,profile_id,payee_id)

    @_guard
    def set_scheme_payee(self,scheme_id,payee_id):
        return self.adapters.set_scheme_payee(scheme_id,payee_id)

    @_guard
    def set_batch_payee(self,batch_id,scheme_id,payee_id):
        return self.adapters.payees.set_batch(batch_id,scheme_id,payee_id)

    def _export_planner(self):
        if self._export_planner_instance is None:
            from .services.export_plan import ExportPlanner
            self._export_planner_instance = ExportPlanner(self.db,self.data_root,self.adapters)
            self._export_planner_instance.recover_jobs()
        return self._export_planner_instance

    @_guard
    def preview_export(self,entry_ids,output_ids=None,options=None):
        self._require_adapter_ready()
        return self._export_planner().preview(entry_ids,output_ids,options)

    @_guard
    def run_export(self,plan_id,output_dir=None):
        self._require_adapter_ready()
        return self._export_planner().run(plan_id,output_dir)

    def cancel_export(self,plan_id):
        # Cancellation may run while the guarded rendering request is in progress.
        try:
            return {"ok":True,"data":self._export_planner().cancel(plan_id)}
        except Exception as exc:
            return {"ok":False,"error":str(exc)}

    def get_export_progress(self,job_id):
        try:
            planner=self._export_planner_instance
            return {"ok":True,"data":planner.get_progress(job_id) if planner else None}
        except Exception as exc:
            return {"ok":False,"error":str(exc)}

    @_guard
    def list_export_jobs(self):
        return self._export_planner().list_jobs()

    @_guard
    def get_export_job(self,job_id):
        return self._export_planner().get_job(job_id)

    @_guard
    def regenerate_export(self,job_id):
        return self._export_planner().regenerate(job_id)
