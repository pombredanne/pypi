''' Implements a store of disutils PKG-INFO entries, keyed off name, version.
'''
import sys, os, re, psycopg, time, sha, random, types, math, stat, errno
from distutils.version import LooseVersion
import logging

def enumerate(sequence):
    return [(i, sequence[i]) for i in range(len(sequence))]
    

chars = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'

dist_file_types = [
    ('sdist',            'Source'),
    ('bdist_dumb',       '"dumb" binary'),
    ('bdist_rpm',        'RPM'),
    ('bdist_wininst',    'MS Windows installer'),
]

class ResultRow:
    def __init__(self, cols, info=None):
        self.cols = cols
        self.cols_d = {}
        for i, col in enumerate(cols):
            self.cols_d[col] = i
        self.info = info
    def __getitem__(self, item):
        if isinstance(item, int):
            return self.info[item]
        return self.info[self.cols_d[item]]
    def items(self):
        return [(col, self.info[i]) for i, col in enumerate(self.cols)]
    def keys(self):
        return self.cols
    def values(self):
        return self.info

def Result(cols, sequence):
    return [ResultRow(cols, item) for item in iter(sequence)]

class StorageError(Exception):
    pass

class Store:
    ''' Store info about packages, and allow query and retrieval.

        XXX update schema info ...
            Packages are unique by (name, version).
    '''
    def __init__(self, config):
        self.config = config
        self.username = None
        self.userip = None
        self._conn = None
        self._cursor = None

    def store_package(self, name, version, info):
        ''' Store info about the package to the database.

        If the name doesn't exist, we add a new package with the current
        user as the Owner of the package.

        If the version doesn't exist, we add a new release, hiding all
        previous releases.

        If the name and version do exist, we just edit (in place) and add a
        journal entry.
        '''
        date = time.strftime('%Y-%m-%d %H:%M:%S')

        cursor = self.get_cursor()
        # see if we're inserting or updating a package
        if not self.has_package(name):
            # insert the new package entry
            sql = 'insert into packages (name) values (%s)'
            cursor.execute(sql, (name, ))

            # journal entry
            cursor.execute('''insert into journals (name, version, action,
                submitted_date, submitted_by, submitted_from) values
                (%s, %s, %s, %s, %s, %s)''', (name, None, 'create', date,
                self.username, self.userip))

            # first person to add an entry may be considered owner - though
            # make sure they don't already have the Role (this might just
            # be a new version, or someone might have already given them
            # the Role)
            if not self.has_role('Owner', name):
                self.add_role(self.username, 'Owner', name)

        # extract the Trove classifiers
        classifiers = info.get('classifiers', [])
        classifiers.sort()

        # now see if we're inserting or updating a release
        message = None
        relationships = {}
        old_cifiers = []
        if self.has_release(name, version):
            # figure the changes
            existing = self.get_package(name, version)

            # handle the special vars that most likely won't have been
            # submitted
            for k in ('_pypi_ordering', '_pypi_hidden'):
                if not info.has_key(k):
                    info[k] = existing[k]

            # figure which cols in the table to update, if any
            specials = 'name version'.split()
            old = []
            cols = []
            vals = []
            for k, v in existing.items():
                if not info.has_key(k):
                    continue
                if k not in specials and info.get(k, None) != v:
                    old.append(k)
                    cols.append(k)
                    vals.append(info[k])
            vals.extend([name, version])

            # get old classifiers list
            old_cifiers = self.get_release_classifiers(name, version)
            old_cifiers.sort()
            if info.has_key('classifiers') and old_cifiers != classifiers:
                old.append('classifiers')

            # get old classifiers list
            for col in ('requires', 'provides', 'obsoletes'):
                relationships[col] = self.get_release_relationships(name,
                    version, col)
                relationships[col].sort()
                new_val = info.get(col, [])
                new_val.sort()
                if info.has_key(col) and relationships[col] != new_val:
                    old.append(col)

            # no update when nothing changes
            if not old:
                return None

            # create the journal/user message
            message = 'update %s'%', '.join(old)

            # update
            if cols:
                cols = ','.join(['%s=%%s'%x for x in cols])
                cursor.execute('''update releases set %s where name=%%s
                    and version=%%s'''%cols, vals)

            # journal the update
            cursor.execute('''insert into journals (name, version,
                action, submitted_date, submitted_by, submitted_from)
                values (%s, %s, %s, %s, %s, %s)''', (name, version, message,
                date, self.username, self.userip))
        else:
            # round off the information (make sure name and version are in
            # the info dict)
            info['name'] = name
            info['version'] = version

            # figure the ordering
            info['_pypi_ordering'] = self.fix_ordering(name, version)

            # perform the insert
            cols = 'name version author author_email maintainer maintainer_email home_page license summary description keywords platform download_url _pypi_ordering _pypi_hidden'.split()
            args = tuple([info.get(k, None) for k in cols])
            params = ','.join(['%s']*len(cols))
            scols = ','.join(cols)
            sql = 'insert into releases (%s) values (%s)'%(scols, params)
            cursor.execute(sql, args)

            # journal entry
            cursor.execute('''insert into journals (name, version, action,
                submitted_date, submitted_by, submitted_from) values
                (%s, %s, %s, %s, %s, %s)''', (name, version, 'new release',
                date, self.username, self.userip))

            # first person to add an entry may be considered owner - though
            # make sure they don't already have the Role (this might just
            # be a new version, or someone might have already given them
            # the Role)
            if not self.has_role('Owner', name):
                self.add_role(self.username, 'Owner', name)

            # hide all other releases of this package
            cursor.execute('update releases set _pypi_hidden=%s where '
                'name=%s and version <> %s', ('TRUE', name, version))

        # handle trove information
        if info.has_key('classifiers') and old_cifiers != classifiers:
            cursor.execute('delete from release_classifiers where name=%s'
                ' and version=%s', (name, version))
            for classifier in classifiers:
                cursor.execute('select id from trove_classifiers where'
                    ' classifier=%s', (classifier, ))
                trove_id = cursor.fetchone()[0]
                cursor.execute('insert into release_classifiers '
                    '(name, version, trove_id) values (%s, %s, %s)',
                    (name, version, trove_id))

        # handle relationship specifiers
        for col in ('requires', 'provides', 'obsoletes'):
            if not info.has_key(col) or relationships.get(col, []) == info[col]:
                continue
            cursor.execute('''delete from release_%s where name=%%s
                and version=%%s'''%col, (name, version))
            for specifier in info[col]:
                cursor.execute('''insert into release_%s (name, version,
                    specifier) values (%%s, %%s, %%s)'''%col, (name,
                    version, specifier))

        return message

    def fix_ordering(self, name, new_version=None):
        ''' Fix the _pypi_ordering column for a package's releases.

            If "new_version" is supplied, insert it into the sequence and
            return the ordering value for it.
        '''
        cursor = self.get_cursor()
        # load up all the version strings for this package and sort them
        cursor.execute(
            'select version,_pypi_ordering from releases where name=%s',
            (name,))
        l = []
        o = {}
        for version, ordering in cursor.fetchall():
            o[version] = ordering
            l.append(LooseVersion(version))
        if new_version is not None:
            l.append(LooseVersion(new_version))
        l.sort()
        n = len(l)

        # most packages won't need to renumber if we give them 100 releases
        max = 10 ** min(math.ceil(math.log10(n)), 2)

        # figure the ordering values for the releases
        for i in range(n):
            v = l[i].vstring
            order = max+i
            if v == new_version:
                new_version = order
            elif order != o[v]:
                # ordering has changed, update
                cursor.execute('''update releases set _pypi_ordering=%s
                    where name=%s and version=%s''', (order, name, v))

        # return the ordering for this release
        return new_version

    def has_package(self, name):
        ''' Determine whether the package exists in the database.

            Returns true/false.
        '''
        cursor = self.get_cursor()
        sql = 'select count(*) from packages where name=%s'
        cursor.execute(sql, (name, ))
        return int(cursor.fetchone()[0])

    def has_release(self, name, version):
        ''' Determine whether the release exists in the database.

            Returns true/false.
        '''
        cursor = self.get_cursor()
        sql = 'select count(*) from releases where name=%s and version=%s'
        cursor.execute(sql, (name, version))
        return int(cursor.fetchone()[0])

    def get_package(self, name, version):
        ''' Retrieve info about the package from the database.

            Returns a mapping with the package info.
        '''
        cursor = self.get_cursor()
        sql = '''select packages.name as name, stable_version, version, author,
                  author_email, maintainer, maintainer_email, home_page,
                  license, summary, description, keywords,
                  platform, download_url, _pypi_ordering, _pypi_hidden
                 from packages, releases
                 where packages.name=%s and version=%s
                  and packages.name = releases.name'''
        cursor.execute(sql, (name, version))
        cols = 'name stable_version version author author_email maintainer maintainer_email home_page license summary description keywords platform download_url _pypi_ordering _pypi_hidden'.split()
        return ResultRow(cols, cursor.fetchone())

    def get_packages(self):
        ''' Fetch the complete list of packages from the database.
        '''
        cursor = self.get_cursor()
        cursor.execute('select * from packages')
        return cursor.fetchall()

    def get_journal(self, name, version):
        ''' Retrieve info about the package from the database.

            Returns a list of the journal entries, giving those entries
            specific to the nominated version and those entries not specific
            to any version.
        '''
        cursor = self.get_cursor()
        # get the generic stuff or the stuff specific to the version
        sql = '''select action, submitted_date, submitted_by, submitted_from
            from journals where name=%s and (version=%s or
           version is NULL) order by submitted_date'''
        cursor.execute(sql, (name, version))
        return Result(('action', 'submitted_date', 'submitted_by',
            'submitted_from'), cursor.fetchall())

    def query_packages(self, spec, andor='and'):
        ''' Find packages that match the spec.

            Return a list of (name, version) tuples.
        '''
        where = []
        for k, v in spec.items():
            if k == '_pypi_hidden':
                if v == '1': v = 'TRUE'
                else: v = 'FALSE'
                where.append("_pypi_hidden = %s"%v)
                continue

            if type(v) != type([]): v = [v]
            # Quote the bits in the string that need it and then embed
            # in a "substring" search. Note - need to quote the '%' so
            # they make it through the python layer happily
            v = ['%%'+s.replace("'", "''")+'%%' for s in v]

            # now add to the where clause
            where.append(' or '.join(["%s LIKE '%s'"%(k, s) for s in v]))

        # construct the SQL
        if where:
            where = ' where ' + ((' %s '%andor).join(where))
        else:
            where = ''

        # do the fetch
        cursor = self.get_cursor()
        sql = '''select name, version, summary from releases %s
            order by lower(name), _pypi_ordering'''%where
        cursor.execute(sql)
        return Result(('name', 'version', 'summary'), cursor.fetchall())

    def get_classifiers(self):
        ''' Fetch the list of valid classifiers from the database.
        '''
        cursor = self.get_cursor()
        cursor.execute('select classifier from trove_classifiers'
            ' order by classifier')
        return [x[0].strip() for x in cursor.fetchall()]

    def get_release_classifiers(self, name, version):
        ''' Fetch the list of classifiers for the release.
        '''
        cursor = self.get_cursor()
        cursor.execute('''select classifier
            from trove_classifiers, release_classifiers where id=trove_id
            and name=%s and version=%s order by classifier''', (name, version))
        return [x[0] for x in cursor.fetchall()]

    def get_release_relationships(self, name, version, relationship):
        ''' Fetch the list of relationships of a particular type, either
            "requires", "provides" or "obsoletes".
        '''
        cursor = self.get_cursor()
        cursor.execute('''select specifier from release_%s where
            name=%%s and version=%%s'''%relationship, (name, version))
        return [x[0] for x in cursor.fetchall()]

    def get_package_roles(self, name):
        ''' Fetch the list of Roles for the package.
        '''
        cursor = self.get_cursor()
        cursor.execute('''select role_name, user_name
            from roles where package_name=%s''', (name, ))
        return cursor.fetchall()

    def latest_releases(self, num=20):
        ''' Fetch "number" latest releases, youngest to oldest.
        '''
        cursor = self.get_cursor()
        cursor.execute('''
            select j.name,j.version,j.submitted_date,r.summary
            from journals j, releases r
            where j.version is not NULL
                  and j.action = 'new release'
                  and j.name = r.name and j.version = r.version
                  and r._pypi_hidden = FALSE
            order by submitted_date desc
        ''')
        d = {}
        l = []
        for name,version,date,summary in cursor.fetchall():
            k = (name,version)
            if d.has_key(k):
                continue
            l.append((name,version,date,summary))
            d[k] = 1   
        return l[:num]

    def latest_updates(self, num=20):
        ''' Fetch "number" latest updates, youngest to oldest.
        '''
        cursor = self.get_cursor()
        cursor.execute('''
            select j.name,j.version,j.submitted_date,r.summary
            from journals j, releases r
            where j.version is not NULL
                  and j.name = r.name and j.version = r.version
                  and r._pypi_hidden = FALSE
            order by submitted_date desc
        ''')
        d = {}
        l = []
        for name,version,date,summary in cursor.fetchall():
            k = (name,version)
            if d.has_key(k):
                continue
            l.append((name,version,date,summary))
            d[k] = 1   
        return l[:num]

    def get_package_releases(self, name, hidden=None):
        ''' Fetch all releses for the package name, including hidden.
        '''
        if hidden is not None:
            hidden = 'and _pypi_hidden = %s'%(str(hidden).upper())
        else:
            hidden = ''
        cursor = self.get_cursor()
        cursor.execute('''
            select r.name as name,r.version as version,j.submitted_date,
                r.summary as summary,_pypi_hidden
            from journals j, releases r
            where j.version is not NULL
                  and j.action = 'new release'
                  and j.name = %%s
                  and r.name = %%s
                  and j.version = r.version
                  %s
            order by submitted_date desc
        '''%hidden, (name, name))
        res = cursor.fetchall()
        if res is None:
            return []
        cols = 'name version submitted_date summary _pypi_hidden'.split()
        return Result(cols, res)

    def remove_release(self, name, version):
        ''' Delete a single release from the database.
        '''
        cursor = self.get_cursor()

        # delete the files
        for file in self.list_files(name, version):
            os.remove(self.gen_file_path(file['python_version'], name,
                file['filename']))

        # delete ancillary table entries
        for tab in ('files', 'provides', 'requires', 'obsoletes',
                'classifiers'):
            cursor.execute('''delete from release_%s where
                name=%%s and version=%%s'''%tab, (name, version))

        # delete releases table entry
        cursor.execute('delete from releases where name=%s and version=%s',
            (name, version))

    def remove_package(self, name):
        ''' Delete an entire package from the database.
        '''
        cursor = self.get_cursor()
        for release in self.get_package_releases(name):
            for file in self.list_files(name, release['version']):
                os.remove(self.gen_file_path(file['python_version'], name,
                    file['filename']))

        # delete ancillary table entries
        for tab in ('files', 'provides', 'requires', 'obsoletes',
                'classifiers'):
            cursor.execute('delete from release_%s where name=%%s'%tab,
                (name, ))

        cursor.execute('delete from releases where name=%s', (name,))
        cursor.execute('delete from journals where name=%s', (name,))
        cursor.execute('delete from roles where package_name=%s', (name,))
        cursor.execute('delete from packages where name=%s', (name,))


    #
    # Users interface
    # 
    def has_user(self, name):
        ''' Determine whether the user exists in the database.

            Returns true/false.
        '''
        cursor = self.get_cursor()
        cursor.execute("select count(*) from users where name='%s'"%name)
        return int(cursor.fetchone()[0])

    def store_user(self, name, password, email, gpg_keyid):
        ''' Store info about the user to the database.

            The "password" argument is passed in cleartext and sha'ed
            before storage.

            New user entries create a rego_otk entry too and return the OTK.
        '''
        cursor = self.get_cursor()
        if self.has_user(name):
            if password:
                # update existing user, including password
                password = sha.sha(password).hexdigest()
                cursor.execute(
                   'update users set password=%s, email=%s, where name=%s',
                    (password, email, name))
            else:
                # update existing user - but not password
                cursor.execute('update users set email=%s where name=%s',
                    (email, name))
            if gpg_keyid is not None:
                cursor.execute('update users set gpg_keyid=%s where name=%s',
                    (gpg_keyid, name))
            return None

        password = sha.sha(password).hexdigest()

        # new user
        cursor.execute(
           'insert into users (name, password, email) values (%s, %s, %s)',
           (name, password, email))
        otk = ''.join([random.choice(chars) for x in range(32)])
        cursor.execute('insert into rego_otk (name, otk) values (%s, %s)',
            (name, otk))
        return otk

    def get_user(self, name):
        ''' Retrieve info about the user from the database.

            Returns a mapping with the user info or None if there is no
            such user.
        '''
        cursor = self.get_cursor()
        cursor.execute('''select name,password,email,gpg_keyid from users where
            name=%s''', (name,))
        return ResultRow(('name', 'password', 'email', 'gpg_keyid'), cursor.fetchone())

    def get_user_by_email(self, email):
        ''' Retrieve info about the user from the database, looked up by
            email address.

            Returns a mapping with the user info or None if there is no
            such user.
        '''
        cursor = self.get_cursor()
        cursor.execute("select * from users where email=%s", (email,))
        return cursor.fetchone()

    def get_users(self):
        ''' Fetch the complete list of users from the database.
        '''
        cursor = self.get_cursor()
        cursor.execute('select name,email from users order by lower(name)')
        return Result(('name', 'email'), cursor.fetchall())

    def has_role(self, role_name, package_name=None, user_name=None):
        ''' Determine whether the current user has the named Role for the
            named package.
        '''
        if user_name is None:
            user_name = self.username
        if user_name is None:
            return 0
        sql = '''select count(*) from roles where user_name=%s and
            role_name=%s and (package_name=%s or package_name is NULL)'''
        cursor = self.get_cursor()
        cursor.execute(sql, (user_name, role_name, package_name))
        return int(cursor.fetchone()[0])

    def add_role(self, user_name, role_name, package_name):
        ''' Add a role to the user for the package.
        '''
        cursor = self.get_cursor()
        cursor.execute('''
            insert into roles (user_name, role_name, package_name)
            values (%s, %s, %s)''', (user_name, role_name, package_name))
        date = time.strftime('%Y-%m-%d %H:%M:%S')
        sql = '''insert into journals (
              name, version, action, submitted_date, submitted_by,
              submitted_from) values (%s, NULL, %s, %s, %s, %s)'''
        cursor.execute(sql, (package_name, 'add %s %s'%(role_name,
            user_name), date, self.username, self.userip))

    def delete_role(self, user_name, role_name, package_name):
        ''' Delete a role for the user for the package.
        '''
        cursor = self.get_cursor()
        cursor.execute('''
            delete from roles where user_name=%s and role_name=%s
            and package_name=%s''', (user_name, role_name, package_name))
        date = time.strftime('%Y-%m-%d %H:%M:%S')
        cursor.execute('''insert into journals (
              name, version, action, submitted_date, submitted_by,
              submitted_from) values (%s, NULL, %s, %s, %s, %s)''',
            (package_name, 'remove %s %s'%(role_name, user_name), date,
            self.username, self.userip))

    def delete_otk(self, otk):
        ''' Delete the One Time Key.
        '''
        self.get_cursor().execute('delete from rego_otk where otk=%s', (otk,))

    def get_otk(self, name):
        ''' Retrieve the One Time Key for the user.
        '''
        cursor = self.get_cursor()
        cursor.execute("select * from rego_otk where name='%s'"%name)
        res = cursor.fetchone()
        if res is None:
            return ''
        return res['otk']

    def user_packages(self, user):
        ''' Retrieve package info for all packages of a user
        '''
        cursor = self.get_cursor()
        sql = '''select distinct(package_name),lower(package_name) from roles
                 where roles.user_name=%s and package_name is not NULL
                 order by lower(package_name)'''
        cursor.execute(sql, (user,))
        res = cursor.fetchall()
        if res is None:
            return []
        return res

    #
    # File handling
    #
    def gen_file_url(self, pyversion, name, filename):
        '''Generate the URL for a given file download.'''
        return os.path.join(self.config.files_url, pyversion,
            name[0], name, filename)

    def gen_file_path(self, pyversion, name, filename):
        '''Generate the path to the file on disk.'''
        return os.path.join(self.config.database_files_dir, pyversion,
            name[0], name, filename)

    def add_file(self, name, version, content, md5_digest, filetype,
            pyversion, comment, filename):
        '''Add to the database and store the content to disk.'''
        # add database entry
        cursor = self.get_cursor()
        sql = '''insert into release_files (name, version, python_version,
            packagetype, comment_text, filename, md5_digest) values
            (%s, %s, %s, %s, %s, %s, %s)'''
        cursor.execute(sql, (name, version, pyversion, filetype,
            comment, filename, md5_digest))

        # store file to disk
        filepath = self.gen_file_path(pyversion, name, filename)
        dirpath = os.path.split(filepath)[0]
        if not os.path.exists(dirpath):
            os.makedirs(dirpath)
        f = open(filepath, 'wb')
        try:
            f.write(content)
        finally:
            f.close()

        # add journal entry
        date = time.strftime('%Y-%m-%d %H:%M:%S')
        cursor.execute('''insert into journals (
              name, version, action, submitted_date, submitted_by,
              submitted_from) values (%s, NULL, %s, %s, %s, %s)''',
            (name, 'add %s file %s'%(pyversion, filename), date,
            self.username, self.userip))

    def list_files(self, name, version):
        cursor = self.get_cursor()
        sql = '''select packagetype, python_version, comment_text,
            filename, md5_digest from release_files where name=%s
            and version=%s'''
        cursor.execute(sql, (name, version))
        l = []
        cols = ('packagetype', 'python_version', 'comment_text',
            'filename', 'md5_digest', 'size')
        for pt, pv, ct, fn, m5 in cursor.fetchall():
            path = self.gen_file_path(pv, name, fn)
            try:
                size = os.stat(path)[stat.ST_SIZE]
            except OSError, error:
                if error.errno != errno.ENOENT: raise
                # file not on disk any more - don't list it
                continue
            l.append(ResultRow(cols, (pt, pv, ct, fn, m5, size)))
        return l

    def remove_file(self, digest):
        cursor = self.get_cursor()
        sql = '''select python_version, name, filename from release_files
            where md5_digest=%s'''
        cursor.execute(sql, (digest, ))
        pyversion, name, filename = cursor.fetchone()
        cursor.execute('delete from release_files where md5_digest=%s',
            (digest, ))
        filepath = self.gen_file_path(pyversion, name, filename)
        dirpath = os.path.split(filepath)[0]
        os.remove(filepath)
        if not os.listdir(dirpath):
            os.rmdir(dirpath)

    def get_file_info(self, digest):
        cursor = self.get_cursor()
        sql = '''select python_version, packagetype, name, comment_text,
            filename from release_files where md5_digest=%s'''
        cursor.execute(sql, (digest, ))
        row = cursor.fetchone()
        if not row:
            raise KeyError, 'invalid digest %r'%digest
        return ResultRow(('python_version', 'packagetype', 'name',
            'comment_text', 'filename'), row)

    #
    # Handle the underlying database
    #
    def last_modified(self):
        return os.stat(self.config.database)[stat.ST_MTIME]

    def get_cursor(self):
        if self._cursor is None:
            self.open()
        return self._cursor

    def open(self):
        ''' Open the database, initialising if necessary.
        '''
        # ensure files are group readable and writable
        self._conn = psycopg.connect(database=self.config.database_name,
            user=self.config.database_user)

        cursor = self._cursor = self._conn.cursor()

    def set_user(self, username, userip):
        ''' Set the user who's doing the changes.
        '''
        # now check the user
        if username is not None:
            if self.has_user(username):
                self.username = username
        self.userip = userip

    def setpasswd(self, username, password):
        password = sha.sha(password).hexdigest()
        self.get_cursor().execute('''
            update users set password='%s' where name='%s'
            '''%(password, username))

    def close(self):
        if self._conn is None:
            return
        self._conn.close()
        self._conn = None
        self._cursor = None

    def commit(self):
        if self._conn is None:
            return
        self._conn.commit()

    def rollback(self):
        if self._conn is None:
            return
        self._conn.rollback()
        self._cursor = None

if __name__ == '__main__':
    import config
    cfg = config.Config(sys.argv[1], 'webui')
    store = Store(cfg)
    store.open()
    if sys.argv[2] == 'changepw':
        store.setpasswd(sys.argv[3], sys.argv[4])
        store.commit()
    elif sys.argv[2] == 'adduser':
        otk = store.store_user(sys.argv[3], sys.argv[4], sys.argv[5])
        store.delete_otk(otk)
        store.commit()
    else:
        print "UNKNOWN COMMAND", sys.argv[2]
    store.close()

