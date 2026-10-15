// The MinuteX Setup tab: which records meetings can link to, and what each
// output does per object. A list of objects with a one-line summary each;
// Edit opens one object. Every rule lives in MinuteXSetupController; this
// only shows and edits the choices.
import { LightningElement } from 'lwc';
import { ShowToastEvent } from 'lightning/platformShowToastEvent';
import getSetup from '@salesforce/apex/MinuteXSetupController.getSetup';
import saveSetup from '@salesforce/apex/MinuteXSetupController.saveSetup';

const MODES = [
    { label: 'Never', value: 'none' },
    { label: 'Off', value: 'off' },
    { label: 'On', value: 'on' },
    { label: 'Always', value: 'always' }
];

const CHECK_ICONS = {
    ok: { icon: 'utility:success', variant: 'success' },
    warn: { icon: 'utility:warning', variant: 'warning' },
    info: { icon: 'utility:info', variant: '' }
};

function message(e) {
    return (e && e.body && e.body.message) || (e && e.message) || 'Something went wrong. Try again.';
}

export default class MinutexSetup extends LightningElement {
    modes = MODES;
    rows = [];
    outputs = [];
    available = [];
    connection;
    saved = false;
    loading = true;
    saving = false;
    dirty = false;
    error;
    addChoice;
    openName;

    connectedCallback() {
        this.load();
    }

    async load() {
        this.loading = true;
        this.error = undefined;
        try {
            this.apply(await getSetup());
        } catch (e) {
            this.error = message(e);
        } finally {
            this.loading = false;
        }
    }

    apply(state) {
        this.outputs = state.outputs || [];
        this.available = state.available || [];
        this.connection = state.connection;
        this.saved = state.saved === true;
        this.rows = (state.objects || []).map((o) => this.toRow(o));
        this.dirty = false;
        if (this.openName && !this.rows.some((r) => r.objectName === this.openName)) {
            this.openName = undefined;
        }
    }

    /** One object, with the summary lines the list shows. */
    toRow(o) {
        const cells = this.outputs.map((out) => ({
            key: `${o.objectName}:${out.value}`,
            output: out.value,
            label: out.label,
            mode: (o.modes && o.modes[out.value]) || 'none'
        }));
        const sent = cells.filter((c) => c.mode !== 'none').map((c) =>
            (c.mode === 'always' ? `${c.label} (always)` : c.mode === 'off' ? `${c.label} (off by default)` : c.label));
        return {
            ...o,
            cells,
            mainText: o.canBePrimary ? 'Can be the main record' : 'Linked only: never the main record',
            sendsText: sent.length ? `Receives: ${sent.join(', ')}` : 'Receives nothing',
            // The tasks check matters only when this object receives tasks.
            shownChecks: (o.checks || [])
                .filter((c) => c.key !== 'tasks' || (o.modes && o.modes.tasks && o.modes.tasks !== 'none'))
                .map((c) => ({ ...c, ...(CHECK_ICONS[c.status] || CHECK_ICONS.info) }))
        };
    }

    get ready() {
        return !this.loading && !this.error;
    }

    get notSaved() {
        return !this.saved;
    }

    get noRows() {
        return this.rows.length === 0;
    }

    get current() {
        return this.rows.find((r) => r.objectName === this.openName);
    }

    get showList() {
        return !this.current;
    }

    get saveDisabled() {
        return this.saving || (this.saved && !this.dirty);
    }

    get connectionText() {
        return this.connection ? this.connection.statusText : '';
    }

    get addOptions() {
        const used = new Set(this.rows.map((r) => r.objectName));
        return this.available.filter((a) => !used.has(a.value));
    }

    get addDisabled() {
        return !this.addChoice;
    }

    update(name, change) {
        this.rows = this.rows.map((r) => (r.objectName === name ? this.toRow({ ...r, ...change }) : r));
        this.dirty = true;
    }

    handleAddChoice(event) {
        this.addChoice = event.detail.value;
    }

    handleAdd() {
        const picked = this.available.find((a) => a.value === this.addChoice);
        if (!picked) return;
        const modes = {};
        this.outputs.forEach((out) => { modes[out.value] = out.defaultMode; });
        const label = picked.label.replace(/ \([^)]*\)$/, '');
        this.rows = [...this.rows, this.toRow({
            objectName: picked.value, label, canBePrimary: true, modes,
            relatedListReady: picked.relatedListReady,
            checks: picked.checks
        })];
        this.addChoice = undefined;
        this.dirty = true;
        this.openName = picked.value;   // straight to its settings
    }

    handleOpen(event) {
        this.openName = event.target.dataset.object;
    }

    handleBack() {
        this.openName = undefined;
    }

    handleRemove(event) {
        const name = event.target.dataset.object;
        this.rows = this.rows.filter((r) => r.objectName !== name);
        this.dirty = true;
    }

    handlePrimary(event) {
        this.update(this.openName, { canBePrimary: event.target.checked });
    }

    handleMode(event) {
        const row = this.current;
        if (!row) return;
        this.update(row.objectName, { modes: { ...row.modes, [event.target.dataset.output]: event.detail.value } });
    }

    async handleSave() {
        this.saving = true;
        try {
            const payload = this.rows.map((r) => ({ objectName: r.objectName, canBePrimary: r.canBePrimary, modes: r.modes }));
            this.apply(await saveSetup({ payloadJson: JSON.stringify(payload) }));
            this.dispatchEvent(new ShowToastEvent({
                title: 'Saved', message: 'MinuteX picks up these settings by itself.', variant: 'success'
            }));
        } catch (e) {
            this.dispatchEvent(new ShowToastEvent({ title: "Couldn't save", message: message(e), variant: 'error' }));
        } finally {
            this.saving = false;
        }
    }
}
