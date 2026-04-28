-- Preserve the 13 users from the 2025-11-25 phpMyAdmin dump.
-- Explicit researcher_id values so any foreign-key references (legacy
-- history rows re-imported later, session cookies, etc.) keep pointing
-- to the same people. The setval() at the bottom advances the SERIAL
-- sequence past the highest id so future INSERTs don't collide.

INSERT INTO users
    (researcher_id, first_name, last_name, email,                          password,        role,         registered_date) VALUES
    (1,             'Alice',      'Johnson', 'alice.johnson@example.com',   'password123',   'Researcher', '2025-09-23 17:49:25'),
    (2,             'Bob',        'Smith',   'bob.smith@example.com',       'password123',   'Admin',      '2025-09-23 17:49:25'),
    (4,             'Diana',      'Lopez',   'diana.lopez@example.com',     'password123',   'Researcher', '2025-09-23 17:49:25'),
    (5,             'Ethan',      'Wright',  'ethan.wright@example.com',    'password123',   'Admin',      '2025-09-23 17:49:25'),
    (6,             'Fiona',      'Clark',   'fiona.clark@example.com',     'password123',   'Researcher', '2025-09-23 17:49:25'),
    (7,             'George',     'Hill',    'george.hill@example.com',     'password123',   'Researcher', '2025-09-23 17:49:25'),
    (8,             'Hannah',     'Green',   'hannah.green@example.com',    'password123',   'Admin',      '2025-09-23 17:49:25'),
    (10,            'Julia',      'Davis',   'julia.davis@example.com',     'password123',   'Researcher', '2025-09-23 17:49:25'),
    (11,            'rahknov',    'tubat',   'rahknov18@example.com',       'password123',   'Researcher', '2025-09-23 18:54:49'),
    (12,            'chuck',      'leclerc', 'chuck@gmail.com',             'hello',         'Researcher', '2025-09-30 12:42:38'),
    (13,            'superadmin', 'admin',   'admin@gmail.com',             '1234',          'Admin',      '2025-09-30 15:45:04'),
    (14,            'hello',      'world',   'helloworld@gmail.com',        '1234',          'Researcher', '2025-09-30 15:46:40'),
    (15,            'Walter',     'white',   'walterwhite@gmail.com',       'gay123',        'Researcher', '2025-11-25 09:30:54');

SELECT setval(
    pg_get_serial_sequence('users', 'researcher_id'),
    (SELECT MAX(researcher_id) FROM users)
);
