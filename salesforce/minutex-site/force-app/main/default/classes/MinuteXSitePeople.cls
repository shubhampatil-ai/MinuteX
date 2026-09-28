/**
 * people_sync: return this org's Contacts and Users, one page at a time, in
 * the provider-neutral shape the MinuteX People Store keeps.
 *
 * MinuteX calls this repeatedly, passing back `next_cursor`, until `done`.
 * Contacts are walked first, then active and inactive Users of type
 * Standard (the org's own licensed staff; portal, guest and automated users
 * are never people a meeting is held with).
 *
 * PAGING BY ID, NOT OFFSET. SOQL OFFSET stops at 2,000 rows; "Id greater
 * than the last one returned" has no ceiling and stays correct while
 * records are added during the walk.
 *
 * Read-only: no DML, so no idempotency record is needed. Every call returns
 * the current state of the org.
 */
public without sharing class MinuteXSitePeople {
    public static final Integer DEFAULT_PAGE_SIZE = 200;
    public static final Integer MAX_PAGE_SIZE = 500;
    static final String PHASE_CONTACT = 'contact';
    static final String PHASE_USER = 'user';
    static final Pattern SF_ID = Pattern.compile('[A-Za-z0-9]{15}([A-Za-z0-9]{3})?');

    public static Map<String, Object> sync(Map<String, Object> envelope) {
        Map<String, Object> payload = MinuteXSiteReceiver.optObj(envelope.get('payload'), 'payload');
        Integer pageSize = pageSize(payload.get('page_size'));

        String phase = PHASE_CONTACT;
        String after = '';
        String cursor = MinuteXSiteReceiver.str(payload.get('cursor'));
        if (String.isNotBlank(cursor)) {
            List<String> parts = cursor.split(':', 2);
            phase = parts[0];
            after = parts.size() > 1 ? parts[1] : '';
            if ((phase != PHASE_CONTACT && phase != PHASE_USER)
                    || (String.isNotBlank(after) && !SF_ID.matcher(after).matches())) {
                throw MinuteXSiteException.forField(400, 'invalid_cursor', 'payload.cursor',
                    'payload.cursor is not a cursor this receiver issued.');
            }
        }

        List<Map<String, Object>> people = new List<Map<String, Object>>();
        String nextCursor = '';
        if (phase == PHASE_CONTACT) {
            List<Contact> contacts = contactsAfter(after, pageSize);
            for (Contact c : contacts) {
                people.add(fromContact(c));
            }
            if (contacts.size() == pageSize) {
                nextCursor = PHASE_CONTACT + ':' + contacts[contacts.size() - 1].Id;
            } else {
                // Contacts exhausted: spend what is left of this page on
                // Users rather than returning a short page and costing
                // MinuteX a round trip.
                phase = PHASE_USER;
                after = '';
            }
        }
        if (phase == PHASE_USER && String.isBlank(nextCursor)) {
            Integer remaining = pageSize - people.size();
            if (remaining > 0) {
                List<User> users = usersAfter(after, remaining);
                for (User u : users) {
                    people.add(fromUser(u));
                }
                if (users.size() == remaining) {
                    nextCursor = PHASE_USER + ':' + users[users.size() - 1].Id;
                }
            } else {
                nextCursor = PHASE_USER + ':';
            }
        }

        return new Map<String, Object>{
            'people' => people,
            'count' => people.size(),
            'next_cursor' => nextCursor,
            'done' => String.isBlank(nextCursor)
        };
    }

    static Integer pageSize(Object raw) {
        if (raw == null) {
            return DEFAULT_PAGE_SIZE;
        }
        if (!(raw instanceof Integer) && !(raw instanceof Long) && !(raw instanceof Decimal)) {
            throw MinuteXSiteException.forField(400, 'invalid_request', 'payload.page_size',
                'payload.page_size must be a number.');
        }
        Integer n = Integer.valueOf(raw);
        return Math.max(1, Math.min(MAX_PAGE_SIZE, n));
    }

    static List<Contact> contactsAfter(String after, Integer n) {
        if (String.isBlank(after)) {
            return [SELECT Id, Name, FirstName, LastName, Email, Phone, Title, Account.Name, LastModifiedDate
                    FROM Contact ORDER BY Id LIMIT :n];
        }
        Id afterId = (Id) after;
        return [SELECT Id, Name, FirstName, LastName, Email, Phone, Title, Account.Name, LastModifiedDate
                FROM Contact WHERE Id > :afterId ORDER BY Id LIMIT :n];
    }

    static List<User> usersAfter(String after, Integer n) {
        if (String.isBlank(after)) {
            return [SELECT Id, Name, FirstName, LastName, Email, Phone, Title, CompanyName, IsActive, LastModifiedDate
                    FROM User WHERE UserType = 'Standard' ORDER BY Id LIMIT :n];
        }
        Id afterId = (Id) after;
        return [SELECT Id, Name, FirstName, LastName, Email, Phone, Title, CompanyName, IsActive, LastModifiedDate
                FROM User WHERE UserType = 'Standard' AND Id > :afterId ORDER BY Id LIMIT :n];
    }

    static Map<String, Object> fromContact(Contact c) {
        return new Map<String, Object>{
            'provider' => 'salesforce',
            'type' => 'contact',
            'external_id' => String.valueOf(c.Id),
            'name' => c.Name,
            'first_name' => c.FirstName,
            'last_name' => c.LastName,
            'email' => c.Email,
            'phone' => c.Phone,
            'title' => c.Title,
            'company' => c.Account == null ? null : c.Account.Name,
            'status' => 'active',
            'source_updated_at' => c.LastModifiedDate
        };
    }

    static Map<String, Object> fromUser(User u) {
        return new Map<String, Object>{
            'provider' => 'salesforce',
            'type' => 'user',
            'external_id' => String.valueOf(u.Id),
            'name' => u.Name,
            'first_name' => u.FirstName,
            'last_name' => u.LastName,
            'email' => u.Email,
            'phone' => u.Phone,
            'title' => u.Title,
            'company' => u.CompanyName,
            'status' => u.IsActive ? 'active' : 'inactive',
            'source_updated_at' => u.LastModifiedDate
        };
    }
}
