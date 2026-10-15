// The MinuteX Meetings component for any record page: the meetings linked
// to this record, newest first. Works on any object, custom ones too, with
// no lookup field: MinuteXRecordMeetings finds them by the record id.
import { LightningElement, api, wire } from 'lwc';
import getMeetings from '@salesforce/apex/MinuteXRecordMeetings.getMeetings';

const FIRST = 5;

export default class MinutexRecordMeetings extends LightningElement {
    @api recordId;
    meetings = [];
    total = 0;
    loading = true;
    error;
    showAll = false;

    @wire(getMeetings, { recordId: '$recordId' })
    received({ data, error }) {
        if (data) {
            this.meetings = (data.meetings || []).map((m) => ({ ...m, url: `/lightning/r/${m.meetingId}/view` }));
            this.total = data.total || 0;
            this.error = undefined;
            this.loading = false;
        } else if (error) {
            this.error = (error.body && error.body.message) || "Couldn't load MinuteX meetings.";
            this.meetings = [];
            this.loading = false;
        }
    }

    get title() {
        return this.total ? `MinuteX Meetings (${this.total})` : 'MinuteX Meetings';
    }

    get empty() {
        return !this.loading && !this.error && this.meetings.length === 0;
    }

    get shown() {
        return this.showAll ? this.meetings : this.meetings.slice(0, FIRST);
    }

    get hasMore() {
        return this.meetings.length > FIRST;
    }

    get moreLabel() {
        if (this.showAll) return 'Show fewer';
        return this.total > this.meetings.length
            ? `Show the latest ${this.meetings.length} of ${this.total}`
            : `Show all ${this.meetings.length}`;
    }

    toggleAll() {
        this.showAll = !this.showAll;
    }
}
