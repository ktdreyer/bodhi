# -*- coding: utf-8 -*-
# Copyright © 2011-2017 Red Hat, Inc. and others.
#
# This file is part of Bodhi.
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.

from collections import defaultdict
from datetime import datetime
from textwrap import wrap
import copy
import hashlib
import json
import os
import re
import rpm
import time
import uuid

from pkgdb2client import PkgDB
from simplemediawiki import MediaWiki
from six.moves.urllib.parse import quote
from sqlalchemy import (and_, Boolean, Column, DateTime, ForeignKey, Integer, or_, Table, Unicode,
                        UnicodeText, UniqueConstraint)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import class_mapper, relationship, backref, validates
from sqlalchemy.orm.exc import NoResultFound
from sqlalchemy.orm.properties import RelationshipProperty
from sqlalchemy.sql import text
from sqlalchemy.types import SchemaType, TypeDecorator, Enum

from bodhi.server import bugs, buildsys, log, mail, notifications, Session
from bodhi.server.config import config
from bodhi.server.exceptions import BodhiException, LockedUpdateException
from bodhi.server.util import (
    avatar as get_avatar, build_evr, flash_log, get_critpath_components,
    get_nvr, get_rpm_header, header, packagename_from_nvr, tokenize, pagure_api_get)
import bodhi.server.util


# http://techspot.zzzeek.org/2011/01/14/the-enum-recipe

class EnumSymbol(object):
    """Define a fixed symbol tied to a parent class."""

    def __init__(self, cls_, name, value, description):
        self.cls_ = cls_
        self.name = name
        self.value = value
        self.description = description

    def __reduce__(self):
        """Allow unpickling to return the symbol
        linked to the DeclEnum class."""
        return getattr, (self.cls_, self.name)

    def __iter__(self):
        return iter([self.value, self.description])

    def __repr__(self):
        return "<%s>" % self.name

    def __unicode__(self):
        return unicode(self.value)

    def __json__(self, request=None):
        return self.value


class EnumMeta(type):
    """Generate new DeclEnum classes."""

    def __init__(cls, classname, bases, dict_):
        cls._reg = reg = cls._reg.copy()
        for k, v in dict_.items():
            if isinstance(v, tuple):
                sym = reg[v[0]] = EnumSymbol(cls, k, *v)
                setattr(cls, k, sym)
        return type.__init__(cls, classname, bases, dict_)

    def __iter__(cls):
        return iter(cls._reg.values())


class DeclEnum(object):
    """Declarative enumeration."""

    __metaclass__ = EnumMeta
    _reg = {}

    @classmethod
    def from_string(cls, value):
        try:
            return cls._reg[value]
        except KeyError:
            raise ValueError("Invalid value for %r: %r" % (cls.__name__, value))

    @classmethod
    def values(cls):
        return cls._reg.keys()

    @classmethod
    def db_type(cls):
        return DeclEnumType(cls)


class DeclEnumType(SchemaType, TypeDecorator):
    def __init__(self, enum):
        self.enum = enum
        self.impl = Enum(
            *enum.values(),
            name="ck%s" % re.sub('([A-Z])', lambda m: "_" + m.group(1).lower(), enum.__name__))

    def _set_table(self, table, column):
        self.impl._set_table(table, column)

    def copy(self):
        return DeclEnumType(self.enum)

    def process_bind_param(self, value, dialect):
        """
        :type value:   bodhi.server.models.enum.EnumSymbol
        :type dialect: sqlalchemy.engine.default.DefaultDialect
        """
        if value is None:
            return None
        return value.value

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return self.enum.from_string(value.strip())

    def create(self, bind=None, checkfirst=False):
        """Issue CREATE ddl for this type, if applicable."""
        super(DeclEnumType, self).create(bind, checkfirst)
        t = self.dialect_impl(bind.dialect)
        if t.impl.__class__ is not self.__class__ and isinstance(t, SchemaType):
            t.impl.create(bind=bind, checkfirst=checkfirst)

    def drop(self, bind=None, checkfirst=False):
        """Issue DROP ddl for this type, if applicable."""
        super(DeclEnumType, self).drop(bind, checkfirst)
        t = self.dialect_impl(bind.dialect)
        if t.impl.__class__ is not self.__class__ and isinstance(t, SchemaType):
            t.impl.drop(bind=bind, checkfirst=checkfirst)


class BodhiBase(object):
    """
    Base class for the SQLAlchemy model base class.

    Attributes:
        __exclude_columns__ (tuple): A list of columns to exclude from JSON
        __include_extras__ (tuple): A list of methods or attrs to include in JSON
        __get_by__ (tuple): A list of columns that :meth:`.get` will query.
        id (int): An integer id that serves as the default primary key.
        query (sqlalchemy.orm.query.Query): a class property which produces a
            Query object against the class and the current Session when called.
    """
    __exclude_columns__ = ('id',)
    __include_extras__ = tuple()
    __get_by__ = ()

    id = Column(Integer, primary_key=True)

    query = Session.query_property()

    @classmethod
    def get(cls, id, db):
        return db.query(cls).filter(or_(
            getattr(cls, col) == id for col in cls.__get_by__
        )).first()

    def __getitem__(self, key):
        return getattr(self, key)

    def __repr__(self):
        return '<{0} {1}>'.format(self.__class__.__name__, self.__json__())

    def __json__(self, request=None, anonymize=False):
        return self._to_json(self, request=request, anonymize=anonymize)

    @classmethod
    def _to_json(cls, obj, seen=None, request=None, anonymize=False):
        if not seen:
            seen = []
        if not obj:
            return

        exclude = getattr(obj, '__exclude_columns__', [])
        properties = list(class_mapper(type(obj)).iterate_properties)
        rels = [p.key for p in properties if type(p) is RelationshipProperty]
        attrs = [p.key for p in properties if p.key not in rels]
        d = dict([(attr, getattr(obj, attr)) for attr in attrs
                  if attr not in exclude and not attr.startswith('_')])

        extras = getattr(obj, '__include_extras__', [])
        for name in extras:
            attribute = getattr(obj, name)
            if callable(attribute):
                attribute = attribute(request)
            d[name] = attribute

        for attr in rels:
            if attr in exclude:
                continue
            target = getattr(type(obj), attr).property.mapper.class_
            if target in seen:
                continue
            d[attr] = cls._expand(obj, getattr(obj, attr), seen, request)

        for key, value in d.iteritems():
            if isinstance(value, datetime):
                d[key] = value.strftime('%Y-%m-%d %H:%M:%S')
            if isinstance(value, EnumSymbol):
                d[key] = unicode(value)

        # If explicitly asked to, we will overwrite some fields if the
        # corresponding condition of each evaluates to True.
        # This is primarily for anonymous Comments.  We want to serialize
        # authenticated FAS usernames in the 'author' field, but we want to
        # scrub out anonymous users' email addresses.
        if anonymize:
            for key1, key2 in getattr(obj, '__anonymity_map__', {}).items():
                if getattr(obj, key2):
                    d[key1] = 'anonymous'

        return d

    @classmethod
    def _expand(cls, obj, relation, seen, req):
        """ Return the to_json or id of a sqlalchemy relationship. """
        if hasattr(relation, 'all'):
            relation = relation.all()
        if hasattr(relation, '__iter__'):
            return [cls._expand(obj, item, seen, req) for item in relation]
        if type(relation) not in seen:
            return cls._to_json(relation, seen + [type(obj)], req)
        else:
            return relation.id

    @classmethod
    def grid_columns(cls):
        columns = []
        exclude = getattr(cls, '__exclude_columns__', [])
        for col in cls.__table__.columns:
            if col.name in exclude:
                continue
            columns.append(col.name)
        return columns

    @classmethod
    def find_polymorphic_child(cls, identity):
        """ Find a child of a polymorphic base class.

        For example, given the base Package class and the 'rpm' identity, this
        class method should return the RpmPackage class.

        This is accomplished by iterating over all classes in scope.
        Limiting that to only those which are an extension of the given base
        class.  Among those, return the one whose polymorphic_identity matches
        the value given.  If none are found, then raise a NameError.

        Arguments:
        identity -- An instance of EnumSymbol used to identify the child.
        """

        if not isinstance(identity, EnumSymbol):
            raise TypeError("%r is not an instance of EnumSymbol" % identity)

        if 'polymorphic_on' not in getattr(cls, '__mapper_args__', {}):
            raise KeyError("%r is not a polymorphic model." % cls)

        classes = (c for c in globals().values() if isinstance(c, type))
        children = (c for c in classes if issubclass(c, cls))
        for child in children:
            candidate = child.__mapper_args__.get('polymorphic_identity')
            if candidate is identity:
                return child

        error = "Found no child of %r with identity %r"
        raise NameError(error % (cls, identity))

    def update_relationship(self, name, model, data, db):
        """Add items to or remove items from a many-to-many relationship

        :name: The name of the relationship column on self, as well as
               the key in `data`
        :model: The model class of the relationship that we're updating
        :data: A dict containing the key `name` with a list of values

        Returns a three-tuple of lists, `new`, `same`, and `removed` indicating
        which items have been added and removed, and which remain unchanged.
        """

        rel = getattr(self, name)
        items = data.get(name)
        new, same, removed = [], copy.copy(items), []
        if items:
            for item in items:
                obj = model.get(item, db)
                if not obj:
                    obj = model(name=item)
                    db.add(obj)
                if obj not in rel:
                    rel.append(obj)
                    new.append(item)
                    same.remove(item)

            for item in rel:
                if item.name not in items:
                    log.info('Removing %r from %r', item, self)
                    rel.remove(item)
                    removed.append(item.name)

        return new, same, removed


Base = declarative_base(cls=BodhiBase)
metadata = Base.metadata


##
#  Enumerated type declarations
##
class ContentType(DeclEnum):
    base = 'base', 'Base'
    rpm = 'rpm', 'RPM'
    module = 'module', 'Module'

    @classmethod
    def infer_content_class(cls, base, build):
        """ Given a base class and a build from koji, identify and return the child
        class associated with the appropriate ContentType.

        For example, given the Package base class and a normal koji build, return
        the RpmPackage model class. Or, given the Build base class and a container
        build, return the ContainerBuild model class.

        Arguments:
        base -- A base model class.
        build -- A dict of information from the build system (koji).
        """

        # Default value.  Overridden below if we find markers in the build info
        identity = cls.rpm

        extra = build.get('extra') or {}
        if 'module' in extra.get('typeinfo', {}):
            identity = cls.module
        elif 'container_koji_task_id' in extra:
            # TODO - implement containers as yet another content type someday.
            raise NotImplementedError("Inferred type 'container' is unhandled.")

        return base.find_polymorphic_child(identity)


class UpdateStatus(DeclEnum):
    pending = 'pending', 'pending'
    testing = 'testing', 'testing'
    stable = 'stable', 'stable'
    unpushed = 'unpushed', 'unpushed'
    obsolete = 'obsolete', 'obsolete'
    processing = 'processing', 'processing'


class TestGatingStatus(DeclEnum):
    """ This class lists the different status the test_gating_status flag can have. """
    # None: Updates created while bodhi has not enabled Greenwave integration
    # waiting: Updates created while bodhi has enabled Greenwave integration
    waiting = 'waiting', 'Waiting'
    # ignored: a decision from Greenwave said that this update does not require any tests
    ignored = 'ignored', 'Ignored'
    # queued: a decision from Greenwave said that the required tests for this update have been
    # queued
    queued = 'queued', 'Queued'
    # running: a decision from Greenwave said that the required tests for this update are running
    running = 'running', 'Running'
    # passed: a decision from Greenwave said that the required tests for this update have passed
    passed = 'passed', 'Passed'
    # failed: a decision from the Greenwave said that the required tests for this update have
    # been failed
    failed = 'failed', 'Failed'


class UpdateType(DeclEnum):
    bugfix = 'bugfix', 'bugfix'
    security = 'security', 'security'
    newpackage = 'newpackage', 'newpackage'
    enhancement = 'enhancement', 'enhancement'


class UpdateRequest(DeclEnum):
    testing = 'testing', 'testing'
    batched = 'batched', 'batched'
    obsolete = 'obsolete', 'obsolete'
    unpush = 'unpush', 'unpush'
    revoke = 'revoke', 'revoke'
    stable = 'stable', 'stable'


class UpdateSeverity(DeclEnum):
    unspecified = 'unspecified', 'unspecified'
    urgent = 'urgent', 'urgent'
    high = 'high', 'high'
    medium = 'medium', 'medium'
    low = 'low', 'low'


class UpdateSuggestion(DeclEnum):
    unspecified = 'unspecified', 'unspecified'
    reboot = 'reboot', 'reboot'
    logout = 'logout', 'logout'


class ReleaseState(DeclEnum):
    disabled = 'disabled', 'disabled'
    pending = 'pending', 'pending'
    current = 'current', 'current'
    archived = 'archived', 'archived'


##
#  Association tables
##

update_bug_table = Table(
    'update_bug_table', metadata,
    Column('update_id', Integer, ForeignKey('updates.id')),
    Column('bug_id', Integer, ForeignKey('bugs.id')))

update_cve_table = Table(
    'update_cve_table', metadata,
    Column('update_id', Integer, ForeignKey('updates.id')),
    Column('cve_id', Integer, ForeignKey('cves.id')))

bug_cve_table = Table(
    'bug_cve_table', metadata,
    Column('bug_id', Integer, ForeignKey('bugs.id')),
    Column('cve_id', Integer, ForeignKey('cves.id')))

user_package_table = Table(
    'user_package_table', metadata,
    Column('user_id', Integer, ForeignKey('users.id')),
    Column('package_id', Integer, ForeignKey('packages.id')))


class Release(Base):
    __tablename__ = 'releases'
    __exclude_columns__ = ('id', 'builds')
    __get_by__ = ('name', 'long_name', 'dist_tag')

    name = Column(Unicode(10), unique=True, nullable=False)
    long_name = Column(Unicode(25), unique=True, nullable=False)
    version = Column(Unicode(5), nullable=False)
    id_prefix = Column(Unicode(25), nullable=False)
    branch = Column(Unicode(10), unique=True, nullable=False)

    dist_tag = Column(Unicode(20), nullable=False)
    stable_tag = Column(UnicodeText, nullable=False)
    testing_tag = Column(UnicodeText, nullable=False)
    candidate_tag = Column(UnicodeText, nullable=False)
    pending_signing_tag = Column(UnicodeText, nullable=False)
    pending_testing_tag = Column(UnicodeText, nullable=False)
    pending_stable_tag = Column(UnicodeText, nullable=False)
    override_tag = Column(UnicodeText, nullable=False)

    state = Column(ReleaseState.db_type(), default=ReleaseState.disabled, nullable=False)

    @property
    def version_int(self):
        regex = re.compile('\D+(\d+)$')
        return int(regex.match(self.name).groups()[0])

    @property
    def mandatory_days_in_testing(self):
        name = self.name.lower().replace('-', '')
        status = config.get('%s.status' % name, None)
        if status:
            days = int(config.get(
                '%s.%s.mandatory_days_in_testing' % (name, status)))
            if days:
                return days
        days = int(config.get('%s.mandatory_days_in_testing' %
                              self.id_prefix.lower().replace('-', '_')))
        if not days:
            log.warn('No mandatory days in testing defined for %s' % self.name)
        return days

    @property
    def collection_name(self):
        """ Return the collection name of this release.  (eg: Fedora EPEL) """
        return ' '.join(self.long_name.split()[:-1])

    @classmethod
    def all_releases(cls, session):
        if cls._all_releases:
            return cls._all_releases
        releases = defaultdict(list)
        for release in session.query(cls).order_by(cls.name.desc()).all():
            releases[release.state.value].append(release.__json__())
        cls._all_releases = releases
        return cls._all_releases
    _all_releases = None

    @classmethod
    def get_tags(cls, session):
        if cls._tag_cache:
            return cls._tag_cache
        data = {'candidate': [], 'testing': [], 'stable': [], 'override': [],
                'pending_testing': [], 'pending_stable': []}
        tags = {}  # tag -> release lookup
        for release in session.query(cls).all():
            for key in data:
                tag = getattr(release, '%s_tag' % key)
                data[key].append(tag)
                tags[tag] = release.name
        cls._tag_cache = (data, tags)
        return cls._tag_cache
    _tag_cache = None

    @classmethod
    def from_tags(cls, tags, session):
        tag_types, tag_rels = cls.get_tags(session)
        for tag in tags:
            release = session.query(cls).filter_by(name=tag_rels[tag]).first()
            if release:
                return release


class TestCase(Base):
    """Test cases from the wiki"""
    __tablename__ = 'testcases'
    __get_by__ = ('name',)

    name = Column(UnicodeText, nullable=False)

    package_id = Column(Integer, ForeignKey('packages.id'))
    # package backref


class Package(Base):
    """
    This model represents a package.

    This model uses single-table inheritance to allow for different package types.

    Attributes:
        name (unicode): A unicode string that uniquely identifies the package.
        requirements (unicode): A unicode string that lists space-separated taskotron test
            results that must pass for this package
        type (int): The polymorphic identity column. This is used to identify what Python
            class to create when loading rows from the database.
        builds (sqlalchemy.orm.collections.InstrumentedList): A list of :class:`Build` objects.
        test_cases (sqlalchemy.orm.collections.InstrumentedList): A list of :class:`TestCase`
            objects.
        committers (sqlalchemy.orm.collections.InstrumentedList): A list of :class:`User` objects
            who are committers.
        stack_id (int): A foreign key to the :class:`Stack`
    """
    __tablename__ = 'packages'
    __get_by__ = ('name',)
    __exclude_columns__ = ('id', 'committers', 'test_cases', 'builds',)

    name = Column(UnicodeText, nullable=False)
    requirements = Column(UnicodeText)
    type = Column(ContentType.db_type(), nullable=False)

    builds = relationship('Build', backref=backref('package', lazy='joined'))
    test_cases = relationship('TestCase', backref='package', order_by="TestCase.id")
    committers = relationship('User', secondary=user_package_table,
                              backref='packages')

    stack_id = Column(Integer, ForeignKey('stacks.id'))

    __mapper_args__ = {
        'polymorphic_on': type,
        'polymorphic_identity': ContentType.base,
    }

    __table_args__ = (
        UniqueConstraint('name', 'type', name='packages_name_and_type_key'),
    )

    def get_pkg_pushers(self, branch, settings):
        """ Pull users who can commit and are watching a package.

        Return two two-tuples of lists:
        * The first tuple is for usernames.  The second tuple is for groups.
        * The first list of the tuple is for committers. The second is for
          watchers.
        """
        watchers = []
        committers = []
        watchergroups = []
        committergroups = []

        pkgdb = PkgDB(settings.get('pkgdb_url'))
        acls = pkgdb.get_package(self.name, branches=branch)

        for package in acls['packages']:
            for acl in package.get('acls', []):
                if acl['status'] == 'Approved':
                    if acl['acl'] == 'watchcommits':
                        name = acl['fas_name']
                        if name.startswith('group::'):
                            watchergroups.append(name.split('::')[1])
                        else:
                            watchers.append(name)
                    elif acl['acl'] == 'commit':
                        name = acl['fas_name']
                        if name.startswith('group::'):
                            committergroups.append(name.split('::')[1])
                        else:
                            committers.append(name)

        return (committers, watchers), (committergroups, watchergroups)

    def get_pkg_committers_from_pagure(self):
        """
        Pull users and groups who can commit on a package in Pagure.

        Returns a tuple with two lists:
        * The first list contains usernames that have commit access.
        * The second list contains FAS group names that have commit access.
        """
        pagure_url = config.get('pagure_url')
        # Pagure uses plural names for its namespaces such as "rpms" except for
        # container
        if self.type.name == 'container':
            namespace = self.type.name
        else:
            namespace = self.type.name + 's'
        package_pagure_url = '{0}/api/0/{1}/{2}'.format(
            pagure_url.rstrip('/'), namespace, self.name)
        package_json = pagure_api_get(package_pagure_url)

        committers = set()
        for access_type in ['owner', 'admin', 'commit']:
            committers = committers | set(
                package_json['access_users'][access_type])

        groups = set()
        for access_type in ['admin', 'commit']:
            for group_name in package_json['access_groups'][access_type]:
                groups.add(group_name)

        # The first list contains usernames with commit access. The second list
        # contains FAS group names with commit access.
        return list(committers), list(groups)

    def fetch_test_cases(self, db):
        """ Get a list of test cases from the wiki """
        if not config.get('query_wiki_test_cases'):
            return

        start = datetime.utcnow()
        log.debug('Querying the wiki for test cases')

        wiki = MediaWiki(config.get('wiki_url'))
        cat_page = 'Category:Package %s test cases' % self.name

        def list_categorymembers(wiki, cat_page, limit=500):
            # Build query arguments and call wiki
            query = dict(action='query', list='categorymembers',
                         cmtitle=cat_page, cmlimit=limit)
            response = wiki.call(query)
            members = [entry['title'] for entry in
                       response.get('query', {}).get('categorymembers', {})
                       if 'title' in entry]

            # Determine whether we need to recurse
            idx = 0
            while True:
                if idx >= len(members) or limit <= 0:
                    break
                # Recurse?
                if members[idx].startswith('Category:') and limit > 0:
                    members.extend(list_categorymembers(wiki, members[idx], limit - 1))
                    members.remove(members[idx])  # remove Category from list
                else:
                    idx += 1

            log.debug('Found the following unit tests: %s', members)
            return members

        for test in set(list_categorymembers(wiki, cat_page)):
            case = db.query(TestCase).filter_by(name=test).first()
            if not case:
                case = TestCase(name=test, package=self)
                db.add(case)
                db.flush()

        log.debug('Finished querying for test cases in %s', datetime.utcnow() - start)

    @validates('builds')
    def validate_builds(self, key, build):
        """
        Validates builds being appended to ensure they are all the same type as the Package.

        This method checks to make sure that all the builds on self.builds have their type attribute
        equal to self.type. The goal is to make sure that Builds of a specific type are only ever
        associated with Packages of the same type and vice-versa. For example, RpmBuilds should only
        ever associate with RpmPackages and never with ModulePackages.

        Args:
            key (str): The field's key, which is un-used in this validator.
            build (Build): The build object which was appended to the list
                of builds.

        Raises:
            ValueError: If the build being appended is not the same type as the package.
        """
        if build.type != self.type:
            raise ValueError(
                (u"A {} Build cannot be associated with a {} Package. A Package's builds must be "
                 u"the same type as the package.").format(
                     build.type.description, self.type.description))
        return build

    def __str__(self):
        x = header(self.name)
        states = {'pending': [], 'testing': [], 'stable': []}
        if len(self.builds):
            for build in self.builds:
                if build.update and build.update.status.description in states:
                    states[build.update.status.description].append(
                        build.update)
        for state in states.keys():
            if len(states[state]):
                x += "\n %s Updates (%d)\n" % (state.title(),
                                               len(states[state]))
                for update in states[state]:
                    x += "    o %s\n" % update.get_title()
        del states
        return x


class ModulePackage(Package):
    """Represents a Module package."""
    __mapper_args__ = {
        'polymorphic_identity': ContentType.module,
    }


class RpmPackage(Package):
    """Represents a RPM package."""
    __mapper_args__ = {
        'polymorphic_identity': ContentType.rpm,
    }


class Build(Base):
    """
    This model represents a specific build of a package.

    This model uses single-table inheritance to allow for different build types.

    Attributes:
        inherited (bool): The purpose of this column is unknown, and it appears to be unused. At the
            time of this writing, there are 112,234 records with inherited set to False and 0 with
            it set to True in the Fedora Bodhi deployment.
        nvr (unicode): The nvr field is really a mapping to the Koji build_target.name field, and is
            used to reference builds in Koji. It is named nvr in reference to the dash-separated
            name-version-release Koji name for RPMs, but it is used by other types as well. At the
            time of this writing, it was not practical to rename nvr since it is used in the REST
            API to reference builds. Thus, it should be thought of as a Koji build identifier rather
            than strictly as an RPM's name, version, and release.
        package_id (int): A foreign key to the Package that this Build is part of.
        release_id (int): A foreign key to the Release that this Build is part of.
        signed (bool): If True, this package has been signed by robosignatory. If False, it has not
            been signed yet.
        update_id (int): A foreign key to the Update that this Build is part of.
        release (sqlalchemy.orm.relationship): A relationship to the Release that this build is part
            of.
        type (int): The polymorphic identify of the row. This is used by sqlalchemy to identify
            which subclass of Build to use.
    """
    __tablename__ = 'builds'
    __exclude_columns__ = ('id', 'package', 'package_id', 'release',
                           'release_id', 'update_id', 'update', 'override')
    __get_by__ = ('nvr',)

    nvr = Column(Unicode(100), unique=True, nullable=False)
    package_id = Column(Integer, ForeignKey('packages.id'))
    release_id = Column(Integer, ForeignKey('releases.id'))
    signed = Column(Boolean, default=False, nullable=False)
    update_id = Column(Integer, ForeignKey('updates.id'))
    ci_url = Column(UnicodeText, default=None, nullable=True)

    release = relationship('Release', backref='builds', lazy=False)

    type = Column(ContentType.db_type(), nullable=False)
    __mapper_args__ = {
        'polymorphic_on': type,
        'polymorphic_identity': ContentType.base,
    }

    def get_url(self):
        """
        Return a the url to details about this build.

        This method appears to be unused and incorrect.

        Return:
            str: A URL for this build.
        """
        return '/' + self.nvr

    def get_tags(self, koji=None):
        """
        Return a list of koji tags for this build.

        Args:
            koji (bodhi.server.buildsys.Buildsysem or koji.ClientSession): A koji client. Defaults
                to calling bodhi.server.buildsys.get_session().
        Return:
            list: A list of strings of the Koji tags on this Build.
        """
        if not koji:
            koji = buildsys.get_session()
        return [tag['name'] for tag in koji.listTags(self.nvr)]

    def untag(self, koji, db):
        """
        Remove all known tags from this build.

        Args:
            koji (bodhi.server.buildsys.Buildsysem or koji.ClientSession): A koji client.
            db (sqlalchemy.orm.session.Session): A database Session.
        """
        tag_types, tag_rels = Release.get_tags(db)
        for tag in self.get_tags():
            if tag in tag_rels:
                log.info('Removing %s tag from %s' % (tag, self.nvr))
                koji.untagBuild(tag, self.nvr)

    def unpush(self, koji):
        """
        Move this build back to the candidate tag and remove any pending tags.

        Args:
            koji (bodhi.server.buildsys.Buildsysem or koji.ClientSession): A koji client.
        """
        log.info('Unpushing %s' % self.nvr)
        release = self.update.release
        for tag in self.get_tags(koji):
            if tag == release.pending_signing_tag:
                log.info('Removing %s tag from %s' % (tag, self.nvr))
                koji.untagBuild(tag, self.nvr)
            if tag == release.pending_testing_tag:
                log.info('Removing %s tag from %s' % (tag, self.nvr))
                koji.untagBuild(tag, self.nvr)
            if tag == release.pending_stable_tag:
                log.info('Removing %s tag from %s' % (tag, self.nvr))
                koji.untagBuild(tag, self.nvr)
            elif tag == release.testing_tag:
                log.info(
                    'Moving %s from %s to %s' % (
                        self.nvr, tag, release.candidate_tag))
                koji.moveBuild(tag, release.candidate_tag, self.nvr)


class ModuleBuild(Build):
    """
    Represents a Module build.

    Note that this model uses single-table inheritance with its Build superclass.

    Attributes:
        nvr (unicode): A unique Koji identifier for the module build.
    """
    __mapper_args__ = {
        'polymorphic_identity': ContentType.module,
    }


class RpmBuild(Build):
    """
    Represents an RPM build.

    Note that this model uses single-table inheritance with its Build superclass.

    Attributes:
        nvr (unicode): A dash (-) separated string of an RPM's name, version, and release (e.g.
            u'bodhi-2.5.0-1.fc26')
        epoch (int): The RPM's epoch.
    """

    epoch = Column(Integer, default=0)

    __mapper_args__ = {
        'polymorphic_identity': ContentType.rpm,
    }

    @property
    def evr(self):
        """
        The RpmBuild's epoch, version, release, all basestrings in a 3-tuple.

        Return:
            tuple: (epoch, version, release)
        """
        if self.epoch:
            name, version, release = get_nvr(self.nvr)
            return (str(self.epoch), version, release)
        else:
            koji_session = buildsys.get_session()
            build = koji_session.getBuild(self.nvr)
            evr = build_evr(build)
            self.epoch = int(evr[0])
            return evr

    def get_latest(self):
        """
        Return the nvr string of the most recent evr that is less than this RpmBuild's nvr. If there
        is no other Build, this returns None.

        Return:
            basestring or None: An nvr string, formatted like RpmBuild.nvr. If there is no other
                Build, returns None.
        """
        koji_session = buildsys.get_session()

        # Grab a list of builds tagged with ``Release.stable_tag`` release
        # tags, and find the most recent update for this package, other than
        # this one.  If nothing is tagged for -updates, then grab the first
        # thing in ``Release.dist_tag``.  We aren't checking
        # ``Release.candidate_tag`` first, because there could potentially be
        # packages that never make their way over stable, so we don't want to
        # generate ChangeLogs against those.
        latest = None
        evr = self.evr
        for tag in [self.update.release.stable_tag, self.update.release.dist_tag]:
            builds = koji_session.getLatestBuilds(
                tag, package=self.package.name)

            # Find the first build that is older than us
            for build in builds:
                new_evr = build_evr(build)
                if rpm.labelCompare(evr, new_evr) < 0:
                    latest = build['nvr']
                    break
            if latest:
                break
        return latest

    def get_changelog(self, timelimit=0):
        """
        Retrieve the RPM changelog of this package since it's last update, or since timelimit.

        Args:
            timelimit (int): Timestamp, specified as the number of seconds since 1970-01-01 00:00:00
                UTC.
        Return:
            str: The RpmBuild's changelog.
        """
        rpm_header = get_rpm_header(self.nvr)
        descrip = rpm_header['changelogtext']
        if not descrip:
            return ""

        who = rpm_header['changelogname']
        when = rpm_header['changelogtime']

        num = len(descrip)
        if num == 1:
            when = [when]

        str = ""
        i = 0
        while (i < num) and (when[i] > timelimit):
            str += '* %s %s\n%s\n' % (time.strftime("%a %b %e %Y",
                                      time.localtime(when[i])), who[i],
                                      descrip[i])
            i += 1
        return str


class Update(Base):
    """
    This model represents an update.

    Attributes:
        title (unicode): The update's title which uniquely identifies the update.
            This is generally an ordered list of the build NVRs contained in the
            update.
        autokarma (bool): A boolean that indicates whether or not the update will
            be automatically pushed when the stable_karma threshold is reached.
        stable_karma (int): A positive integer that indicates the amount of "good"
            karma the update must receive before being automatically marked as stable.
        unstable_karma (int): A positive integer that indicates the amount of "bad"
            karma the update must receive before being automatically marked as unstable.
        requirements (unicode): A list of taskotron tests that must pass for this
            update to be considered stable.
        require_bugs (bool): Indicates whether or not positive feedback needs to be
            provided for the associated bugs before the update can be considered
            stable.
        require_testcases (bool): Indicates whether or not the update requires that
            positive feedback be given on all associated wiki test cases before the
            update can pass to stable. If the update has no associated wiki test cases,
            this option has no effect.
        notes (unicode): Notes about the update. This is a human-readable field that
            describes what the update is for (e.g. the bugs it fixes).
        type (EnumSymbol): The type of the update (e.g. enhancement, bugfix, etc). It
            must be one of the values defined in :class:`UpdateType`.
        status (EnumSymbol): The current status of the update. Possible values include
            'pending' to indicate it is not yet in a repository, 'testing' to indicate it
            is in the testing repository, etc. It must be one of the values defined in
            :class:`UpdateStatus`.
        request (EnumSymbol): The requested status of the update. This must be one of the
            values defined in :class:`UpdateRequest` or ``None``.
        severity (EnumSymbol): The update's severity. This must be one of the values defined
            in :class:`UpdateSeverity`.
        suggest (EnumSymbol): Suggested action a user should take after applying the update.
            This must be one of the values defined in :class:`UpdateSuggestion`.
        locked (bool): Indicates whether or not the update is locked and un-editable.
            This is usually set by the masher because the update is going through a state
            transition.
        pushed (bool): Indicates whether or not the update has been pushed to its requested
            repository.
        critpath (bool): Indicates whether or not the update is for a "critical path"
            :class:`Package`. Critical path packages are packages that are required for
            basic functionality. For example, the kernel :class:`RpmPackage` is a critical
            path package.
        close_bugs (bool): Indicates whether the Bugzilla bugs that this update is related
            to should be closed automatically when the update is pushed to stable.
        date_submitted (DateTime): The date that the update was created.
        date_modified (DateTime): The date the update was last modified or ``None``.
        date_approved (DateTime): The date the update was approved or ``None``.
        date_pushed (DateTime): The date the update was pushed or ``None``.
        date_testing (DateTime): The date the update was placed into the testing repository
            or ``None``.
        date_stable (DateTime): The date the update was placed into the stable repository or
            ``None``.
        date_locked (DateTime): The date the update was locked or ``None``.
        alias (unicode): The update alias (e.g. FEDORA-EPEL-2009-12345).
        old_updateid (unicode): The legacy update ID which has been deprecated.
        release_id (int): A foreign key to the releases ``id``.
        release (Release): The ``Release`` object this update relates to via the ``release_id``.
        comments (sqlalchemy.orm.collections.InstrumentedList): A list of the :class:`Comment`
            objects for this update.
        builds (sqlalchemy.orm.collections.InstrumentedList): A list of :class:`Build` objects
            contained in this update.
        bugs (sqlalchemy.orm.collections.InstrumentedList): A list of :class:`Bug` objects
            associated with this update.
        cves (sqlalchemy.orm.collections.InstrumentedList): A list of :class:`CVE` objects
            associated with this update.
        user_id (int): A foreign key to the :class:`User` that created this update.
        test_gating_status (EnumSymbol): The test gating status of the update. This must be one
            of the values defined in :class:`TestGatingStatus` or ``None``. None indicates that
            Greenwave integration was not enabled when the update was created.
        greenwave_summary_string (unicode): A short summary of the outcome from Greenwave
            (e.g. 2 of 32 required tests failed).
    """
    __tablename__ = 'updates'
    __exclude_columns__ = ('id', 'user_id', 'release_id', 'cves')
    __include_extras__ = ('meets_testing_requirements', 'url',)
    __get_by__ = ('title', 'alias')

    title = Column(UnicodeText, unique=True, default=None, index=True)

    autokarma = Column(Boolean, default=True, nullable=False)
    stable_karma = Column(Integer, nullable=True)
    unstable_karma = Column(Integer, nullable=True)
    requirements = Column(UnicodeText)
    require_bugs = Column(Boolean, default=False)
    require_testcases = Column(Boolean, default=False)

    notes = Column(UnicodeText, nullable=False)  # Mandatory notes

    # Enumerated types
    type = Column(UpdateType.db_type(), nullable=False)
    status = Column(UpdateStatus.db_type(),
                    default=UpdateStatus.pending,
                    nullable=False)
    request = Column(UpdateRequest.db_type())
    severity = Column(UpdateSeverity.db_type(), default=UpdateSeverity.unspecified)
    suggest = Column(UpdateSuggestion.db_type(), default=UpdateSuggestion.unspecified)

    # Flags
    locked = Column(Boolean, default=False)
    pushed = Column(Boolean, default=False)
    critpath = Column(Boolean, default=False)

    # Bug settings
    close_bugs = Column(Boolean, default=True)

    # Timestamps
    date_submitted = Column(DateTime, default=datetime.utcnow, index=True)
    date_modified = Column(DateTime)
    date_approved = Column(DateTime)
    date_pushed = Column(DateTime)
    date_testing = Column(DateTime)
    date_stable = Column(DateTime)
    date_locked = Column(DateTime)

    # eg: FEDORA-EPEL-2009-12345
    alias = Column(Unicode(32), unique=True, nullable=True)

    # deprecated: our legacy update ID
    old_updateid = Column(Unicode(32), default=None)

    # One-to-one relationships
    release_id = Column(Integer, ForeignKey('releases.id'))
    release = relationship('Release', lazy='joined')

    # One-to-many relationships
    comments = relationship('Comment', backref=backref('update', lazy='joined'), lazy='joined',
                            order_by='Comment.timestamp')
    builds = relationship('Build', backref=backref('update', lazy='joined'), lazy='joined',
                          order_by='Build.nvr')

    # Many-to-many relationships
    bugs = relationship('Bug', secondary=update_bug_table, backref='updates')
    cves = relationship('CVE', secondary=update_cve_table, backref='updates')

    user_id = Column(Integer, ForeignKey('users.id'))

    # Greenwave
    test_gating_status = Column(TestGatingStatus.db_type(), default=None, nullable=True)
    greenwave_summary_string = Column(Unicode(255))

    @validates('builds')
    def validate_builds(self, key, build):
        """
        Validates builds being appended to ensure they are all the same type.

        Args:
            key (str): The field's key, which is un-used in this validator.
            build (Build): The build object which was appended to the list
                of builds.

        Raises:
            ValueError: If the build being appended is not the same type as the
                existing builds.
        """
        if not all([type(b) is type(build) for b in self.builds]):
            raise ValueError(u'An update must contain builds of the same type.')
        return build

    @property
    def mandatory_days_in_testing(self):
        """
        Calculate and return how many days an update should be in testing before becoming stable.

        :return: The number of mandatory days in testing.
        :rtype:  int
        """
        if self.critpath:
            return config.get('critpath.stable_after_days_without_negative_karma')

        days = self.release.mandatory_days_in_testing
        return days if days else 0

    @property
    def karma(self):
        """
        Calculate and return the karma for the Update.

        :return: The Update's current karma.
        :rtype:  int
        """
        positive_karma, negative_karma = self._composite_karma
        return positive_karma + negative_karma

    @property
    def _composite_karma(self):
        """
        Calculate and return a 2-tuple of the sum of the positive karma comments, and the sum of the
        negative karma comments. The total karma is simply the sum of the two elements of this
        2-tuple.

        :return: a 2-tuple of (positive_karma, negative_karma)
        :rtype:  tuple
        """
        positive_karma = 0
        negative_karma = 0
        users_counted = set()
        for comment in self.comments_since_karma_reset:
            if comment.karma and not comment.anonymous and comment.user.name not in users_counted:
                # Make sure we only count the last comment this user made
                users_counted.add(comment.user.name)
                if comment.karma > 0:
                    positive_karma += comment.karma
                else:
                    negative_karma += comment.karma

        return positive_karma, negative_karma

    @property
    def comments_since_karma_reset(self):
        """
        Return the comments since the most recent karma reset event, which
        occurs whenever a build is added or removed from an Update.
        :return: an iterable of recent comments
        :rtype: generator
        """
        # We want to traverse the comments in reverse order so we only consider
        # the most recent comments from any given user and only the comments
        # since the most recent karma reset event.
        for comment in reversed(self.comments):
            if (comment.user.name == u'bodhi' and
                    ('New build' in comment.text or 'Removed build' in comment.text)):
                # We only want to consider comments since the most recent karma
                # reset, which happens whenever a build is added or removed
                # from an Update. Since we are traversing the comments in
                # reverse order, once we find one of these comments we can
                # simply exit this loop.
                break
            yield comment

    @staticmethod
    def contains_critpath_component(builds, release_name):
        """
        Determines if there is a critpath component in the builds passed in.
        :param builds: a list of Build objects
        :param release_name: a string representing the name of the release
        such as "f25"
        :return: a boolean determining if the update contains a critpath
        component
        """
        for build in builds:
            # This function call is cached, so there is no need to optimize
            # it here.
            critpath_components = get_critpath_components(
                release_name.lower(), build.package.type.value)
            if build.package.name in critpath_components:
                return True

        return False

    @property
    def greenwave_subject(self):
        """
        Form and return the proper Greenwave API subject field for this Update.

        Returns:
            list: A list of dictionaries that are appropriate to be passed to the Greenwave API
                subject field for a decision about this Update.
        """
        # See discussion on https://pagure.io/greenwave/issue/34 for why we use these subjects.
        subject = [{'item': build.nvr, 'type': 'koji_build'} for build in self.builds]
        subject.append({'item': self.alias, 'type': 'bodhi_update'})
        return subject

    @property
    def greenwave_subject_json(self):
        """
        Form and return the proper Greenwave API subject field for this Update as JSON.

        Returns:
            basestring: A JSON list of objects that are appropriate to be passed to the Greenwave
                API subject field for a decision about this Update.
        """
        return json.dumps(self.greenwave_subject)

    @classmethod
    def new(cls, request, data):
        """ Create a new update """
        db = request.db
        user = User.get(request.user.name, request.db)
        data['user'] = user
        data['title'] = ' '.join([b.nvr for b in data['builds']])
        caveats = []
        data['critpath'] = cls.contains_critpath_component(
            data['builds'], data['release'].name)

        # Create the Bug entities, but don't talk to rhbz yet.  We do that
        # offline in the UpdatesHandler fedmsg consumer now.
        bugs = []
        if data['bugs']:
            for bug_num in data['bugs']:
                bug = db.query(Bug).filter_by(bug_id=bug_num).first()
                if not bug:
                    bug = Bug(bug_id=bug_num)
                    db.add(bug)
                    db.flush()
                bugs.append(bug)
        data['bugs'] = bugs

        # If no requirements are provided, then gather some defaults from the
        # packages of the associated builds.
        # See https://github.com/fedora-infra/bodhi/issues/101
        if not data['requirements']:
            data['requirements'] = " ".join(list(set(sum([
                list(tokenize(pkg.requirements)) for pkg in [
                    build.package for build in data['builds']
                ] if pkg.requirements], []))))

        del(data['edited'])

        req = data.pop("request", UpdateRequest.testing)

        # Create the update
        log.debug("Creating new Update(**data) object.")
        up = Update(**data)

        # Assign the alias before setting the request.
        # Setting the request publishes a fedmsg message, and it is nice to
        # already have the alias there for URL construction and backend update
        # handling.
        log.debug("Assigning alias for new update..")
        up.assign_alias()
        log.debug("Setting request for new update.")
        up.set_request(db, req, request.user.name)

        log.debug("Adding new update to the db.")
        db.add(up)
        log.debug("Triggering db flush for new update.")
        db.flush()

        if config.get('test_gating.required'):
            log.debug(
                'Test gating required is enforced, marking the update as waiting on test gating')
            up.test_gating_status = TestGatingStatus.waiting

        log.debug("Done with Update.new(...)")
        return up, caveats

    @classmethod
    def edit(cls, request, data):
        db = request.db
        buildinfo = request.buildinfo
        koji = request.koji
        up = db.query(Update).filter_by(title=data['edited']).first()
        del(data['edited'])

        caveats = []
        edited_builds = [build.nvr for build in up.builds]

        # Determine which builds have been added
        new_builds = []
        for build in data['builds']:
            if build not in edited_builds:
                if up.locked:
                    raise LockedUpdateException("Can't add builds to a "
                                                "locked update")

                new_builds.append(build)
                name, version, release = buildinfo[build]['nvr']
                package = db.query(Package).filter_by(name=name).first()
                if not package:
                    package = Package(name=name)
                    db.add(package)
                b = db.query(RpmBuild).filter_by(nvr=build).first()
                if not b:
                    b = RpmBuild(nvr=build, package=package)
                    b.release = up.release
                    db.add(b)

                up.builds.append(b)

        # Determine which builds have been removed
        removed_builds = []
        for build in edited_builds:
            if build not in data['builds']:
                if up.locked:
                    raise LockedUpdateException("Can't remove builds from a "
                                                "locked update")

                removed_builds.append(build)
                b = None
                for b in up.builds:
                    if b.nvr == build:
                        break

                b.unpush(koji=request.koji)
                up.builds.remove(b)

                # Expire any associated buildroot override
                if b.override:
                    b.override.expire()
                else:
                    # Only delete the Build entity if it isn't associated with
                    # an override
                    db.delete(b)

        data['critpath'] = cls.contains_critpath_component(
            up.builds, up.release.name)

        del(data['builds'])

        # Comment on the update with details of added/removed builds
        # .. enumerate the builds in markdown format so they're pretty.
        comment = '%s edited this update.' % request.user.name
        if new_builds:
            comment += '\n\nNew build(s):\n'
            for new_build in new_builds:
                comment += "\n- %s" % new_build
        if removed_builds:
            comment += '\n\nRemoved build(s):\n'
            for removed_build in removed_builds:
                comment += "\n- %s" % removed_build
        if new_builds or removed_builds:
            comment += '\n\nKarma has been reset.'
        up.comment(db, comment, karma=0, author=u'bodhi')
        caveats.append({'name': 'builds', 'description': comment})

        data['title'] = ' '.join(sorted([b.nvr for b in up.builds]))

        # Updates with new or removed builds always go back to testing
        if new_builds or removed_builds:
            data['request'] = UpdateRequest.testing

            # Remove all koji tags and change the status back to pending
            if up.status is not UpdateStatus.pending:
                up.unpush(db)
                caveats.append({
                    'name': 'status',
                    'description': 'Builds changed.  Your update is being '
                    'sent back to testing.',
                })

            # Add the pending_signing_tag to all new builds
            for build in new_builds:
                if up.release.pending_signing_tag:
                    koji.tagBuild(up.release.pending_signing_tag, build)
                else:
                    # EL6 doesn't have these, and that's okay...
                    # We still warn in case the config gets messed up.
                    log.warn('%s has no pending_signing_tag' % up.release.name)

        # And, updates with new or removed builds always get their karma reset.
        # https://github.com/fedora-infra/bodhi/issues/511
        if new_builds or removed_builds:
            data['karma_critpath'] = 0

        new_bugs = up.update_bugs(data['bugs'], db)
        del(data['bugs'])

        req = data.pop("request", None)
        if req is not None:
            up.set_request(db, req, request.user.name)

        for key, value in data.items():
            setattr(up, key, value)

        up.date_modified = datetime.utcnow()

        notifications.publish(topic='update.edit', msg=dict(
            update=up, agent=request.user.name, new_bugs=new_bugs))

        return up, caveats

    @property
    def signed(self):
        if not self.release.pending_signing_tag:
            return True
        return all([build.signed for build in self.builds])

    @property
    def content_type(self):
        """ Return the ContentType associated with this Update.

        If the update has no builds, this evaluates to `None`.

        Returns:
            ContentType or None: The content type of this Update or None.
        """
        if self.builds:
            return self.builds[0].type

    @property
    def test_gating_passed(self):
        """
        Returns a boolean representing if this update has passed the test gating.

        Returns:
            bool: Returns True if the Update's test_gating_status property is None, ignored,
                or passed. Otherwise it returns False.
        """
        if self.test_gating_status in (
                None, TestGatingStatus.ignored, TestGatingStatus.passed):
            return True
        return False

    def obsolete_older_updates(self, db):
        """Obsolete any older pending/testing updates.

        If a build is associated with multiple updates, make sure that
        all updates are safe to obsolete, or else just skip it.
        """
        caveats = []
        for build in self.builds:
            for oldBuild in db.query(RpmBuild).join(Update).filter(
                and_(RpmBuild.nvr != build.nvr,
                     Build.package == build.package,
                     Update.locked == False,
                     Update.release == self.release,
                     or_(Update.request == UpdateStatus.testing,
                         Update.request == None),
                     or_(Update.status == UpdateStatus.testing,
                         Update.status == UpdateStatus.pending))
            ).all():
                obsoletable = False
                nvr = get_nvr(build.nvr)
                if rpm.labelCompare(get_nvr(oldBuild.nvr), nvr) < 0:
                    log.debug("%s is newer than %s" % (nvr, oldBuild.nvr))
                    obsoletable = True

                # Ensure that all of the packages in the old update are
                # present in the new one.
                pkgs = [b.package.name for b in self.builds]
                for _build in oldBuild.update.builds:
                    if _build.package.name not in pkgs:
                        obsoletable = False
                        break

                # Warn if you're stomping on another user but don't necessarily
                # obsolete them
                if len(oldBuild.update.builds) != len(self.builds):
                    if oldBuild.update.user.name != self.user.name:
                        caveats.append({
                            'name': 'update',
                            'description': 'Please be aware that there '
                            'is another update in flight owned by %s, '
                            'containing %s.  Are you coordinating with '
                            'them?' % (
                                oldBuild.update.user.name,
                                oldBuild.nvr,
                            )
                        })

                if obsoletable:
                    log.info('%s is obsoletable' % oldBuild.nvr)

                    # Have the newer update inherit the older updates bugs
                    oldbugs = [bug.bug_id for bug in oldBuild.update.bugs]
                    bugs = [bug.bug_id for bug in self.bugs]
                    self.update_bugs(bugs + oldbugs, db)

                    # Also inherit the older updates notes as well and
                    # add a markdown separator between the new and old ones.
                    self.notes += '\n\n----\n\n' + oldBuild.update.notes
                    oldBuild.update.obsolete(db, newer=build)
                    template = ('This update has obsoleted %s, and has '
                                'inherited its bugs and notes.')
                    link = "[%s](%s)" % (oldBuild.nvr,
                                         oldBuild.update.abs_url())
                    self.comment(db, template % link, author=u'bodhi')
                    caveats.append({
                        'name': 'update',
                        'description': template % oldBuild.nvr,
                    })

        return caveats

    def get_tags(self):
        """ Return all koji tags for all builds on this update. """
        return list(set(sum([b.get_tags() for b in self.builds], [])))

    def get_title(self, delim=' ', limit=None, after_limit='…'):
        all_nvrs = map(lambda x: x.nvr, self.builds)
        nvrs = all_nvrs[:limit]
        builds = delim.join(sorted(nvrs)) + (after_limit if limit and len(all_nvrs) > limit else "")
        return builds

    def get_bugstring(self, show_titles=False):
        """Return a space-delimited string of bug numbers for this update """
        val = u''
        if show_titles:
            i = 0
            for bug in self.bugs:
                bugstr = u'%s%s - %s\n' % (
                    i and ' ' * 11 + ': ' or '', bug.bug_id, bug.title)
                val += u'\n'.join(wrap(
                    bugstr, width=67,
                    subsequent_indent=' ' * 11 + ': ')) + '\n'
                i += 1
            val = val[:-1]
        else:
            val = u' '.join([str(bug.bug_id) for bug in self.bugs])
        return val

    def get_cvestring(self):
        """ Return a space-delimited string of CVE ids for this update """
        return u' '.join([cve.cve_id for cve in self.cves])

    def get_bug_karma(self, bug):
        good, bad, seen = 0, 0, set()
        for comment in reversed(self.comments):
            if comment.user.name in seen:
                continue
            seen.add(comment.user.name)
            for feedback in comment.bug_feedback:
                if feedback.bug == bug:
                    if feedback.karma > 0:
                        good += 1
                    elif feedback.karma < 0:
                        bad += 1
        return bad * -1, good

    def get_testcase_karma(self, testcase):
        good, bad, seen = 0, 0, set()
        for comment in reversed(self.comments):
            if comment.user.name in seen:
                continue
            seen.add(comment.user.name)
            for feedback in comment.unique_testcase_feedback:
                if feedback.testcase == testcase:
                    if feedback.karma > 0:
                        good += 1
                    elif feedback.karma < 0:
                        bad += 1
        return bad * -1, good

    def beautify_title(self, amp=False, nvr=False):
        """Will shorten the title according to its length.

        This is used mostly in subject of a update notification email and
        displaying the title in html. If there are 3 or more builds per title
        the title be:

            "package1, package, 2 and XXX more"

        If the "amp" parameter is specified it will replace the and with and
        &amp; html entity

        If the "nvr" parameter is specified it will include name, version and
        release information in package labels.
        """
        def build_label():
            return build.nvr if nvr else packagename_from_nvr(self, build.nvr)

        if len(self.builds) > 2:
            title = ", ".join([build_label() for build in self.builds[:2]])

            if amp:
                title += ", &amp; "
            else:
                title += ", and "
            title += str(len(self.builds) - 2)
            title += " more"
            return title
        else:
            return " and ".join([build_label() for build in self.builds])

    def assign_alias(self):
        """Return a randomly-suffixed update ID.

        This function used to construct update IDs in a monotonic sequence, but
        we ran into race conditions so we do it randomly now.
        """
        prefix = self.release.id_prefix
        year = time.localtime()[0]
        id = hashlib.sha1(str(uuid.uuid4())).hexdigest()[:10]
        alias = u'%s-%s-%s' % (prefix, year, id)
        log.debug('Setting alias for %s to %s' % (self.title, alias))
        self.alias = alias

    def set_request(self, db, action, username):
        """ Attempt to request an action for this update """
        log.debug('Attempting to set request %s' % action)
        notes = []
        if isinstance(action, basestring):
            action = UpdateRequest.from_string(action)
        if self.status and action.description == self.status.description:
            log.info("%s already %s" % (self.title, action.description))
            return
        if action is self.request:
            log.debug("%s has already been submitted to %s" % (self.title,
                                                               self.request.description))
            return

        if self.locked:
            raise LockedUpdateException("Can't change the request on a "
                                        "locked update")

        topic = u'update.request.%s' % action
        if action is UpdateRequest.unpush:
            self.unpush(db)
            self.comment(db, u'This update has been unpushed.', author=username)
            notifications.publish(topic=topic, msg=dict(
                update=self, agent=username))
            flash_log("%s has been unpushed." % self.title)
            return
        elif action is UpdateRequest.obsolete:
            self.obsolete(db)
            flash_log("%s has been obsoleted." % self.title)
            notifications.publish(topic=topic, msg=dict(
                update=self, agent=username))
            return

        # If status is pending going to testing request and action is revoke,
        # set the status to unpushed
        elif self.status is UpdateStatus.pending and self.request is UpdateRequest.testing \
                and action is UpdateRequest.revoke:
            self.status = UpdateStatus.unpushed
            self.revoke()
            flash_log("%s has been revoked." % self.title)
            notifications.publish(topic=topic, msg=dict(
                update=self, agent=username))
            return

        # If status is testing going to stable request and action is revoke,
        # keep the status at testing
        elif self.request in (UpdateRequest.stable, UpdateRequest.batched) and \
                self.status is UpdateStatus.testing and action is UpdateRequest.revoke:
            self.status = UpdateStatus.testing
            self.revoke()
            flash_log("%s has been revoked." % self.title)
            notifications.publish(topic=topic, msg=dict(
                update=self, agent=username))
            return

        elif action is UpdateRequest.revoke:
            self.revoke()
            flash_log("%s has been revoked." % self.title)
            notifications.publish(topic=topic, msg=dict(
                update=self, agent=username))
            return

        # Disable pushing critical path updates for pending releases directly to stable
        if action in (UpdateRequest.stable, UpdateRequest.batched) and self.critpath:
            if config.get('critpath.num_admin_approvals') is not None:
                if not self.critpath_approved:
                    stern_note = (
                        'This critical path update has not yet been approved for pushing to the '
                        'stable repository.  It must first reach a karma of %s, consisting of %s '
                        'positive karma from proventesters, along with %d additional karma from '
                        'the community. Or, it must spend %s days in testing without any negative '
                        'feedback')
                    stern_note = stern_note % (
                        config.get('critpath.min_karma'),
                        config.get('critpath.num_admin_approvals'),
                        (config.get('critpath.min_karma') -
                            config.get('critpath.num_admin_approvals')),
                        config.get('critpath.stable_after_days_without_negative_karma'))
                    if config.get('test_gating.required'):
                        stern_note += ' Additionally, it must pass automated tests.'
                    notes.append(stern_note)

                    if self.status is UpdateStatus.testing:
                        self.request = None
                        raise BodhiException('. '.join(notes))
                    else:
                        log.info('Forcing critical path update into testing')
                        action = UpdateRequest.testing

        # Ensure this update meets the minimum testing requirements
        flash_notes = ''
        if action in (UpdateRequest.stable, UpdateRequest.batched) and not self.critpath:
            # Check if we've met the karma requirements
            if (self.stable_karma not in (None, 0) and self.karma >=
                    self.stable_karma) or self.critpath_approved:
                log.debug('%s meets stable karma requirements' % self.title)
            else:
                # If we haven't met the stable karma requirements, check if it
                # has met the mandatory time-in-testing requirements
                if self.mandatory_days_in_testing:
                    if not self.met_testing_requirements and \
                       not self.meets_testing_requirements:
                        if self.release.id_prefix == "FEDORA-EPEL":
                            flash_notes = config.get('not_yet_tested_epel_msg')
                        else:
                            flash_notes = config.get('not_yet_tested_msg')
                        if self.status is UpdateStatus.testing:
                            self.request = None
                            raise BodhiException(flash_notes)
                        elif self.request is UpdateRequest.testing:
                            raise BodhiException(flash_notes)
                        else:
                            action = UpdateRequest.testing

        # Add the appropriate 'pending' koji tag to this update, so tools like
        # AutoQA can mash repositories of them for testing.
        if action is UpdateRequest.testing:
            self.add_tag(self.release.pending_signing_tag)
        elif action is UpdateRequest.stable:
            self.add_tag(self.release.pending_stable_tag)

        # If an obsolete/unpushed build is being re-submitted, return
        # it to the pending state, and make sure it's tagged as a candidate
        if self.status in (UpdateStatus.obsolete, UpdateStatus.unpushed):
            self.status = UpdateStatus.pending
            if self.release.candidate_tag not in self.get_tags():
                self.add_tag(self.release.candidate_tag)

        self.request = action

        notes = notes and '. '.join(notes) + '.' or ''
        flash_notes = flash_notes and '. %s' % flash_notes
        flash_log(
            "%s has been submitted for %s. %s%s" % (
                self.title, action.description, notes, flash_notes))
        self.comment(db, u'This update has been submitted for %s by %s. %s' % (
            action.description, username, notes), author=u'bodhi')
        topic = u'update.request.%s' % action
        notifications.publish(topic=topic, msg=dict(update=self, agent=username))

    def add_tag(self, tag):
        """ Add a koji tag to all builds in this update """
        log.debug('Adding tag %s to %s' % (tag, self.title))
        if not tag:
            log.warn("Not adding builds of %s to empty tag" % self.title)
            return []  # An empty iterator in place of koji multicall

        koji = buildsys.get_session()
        koji.multicall = True
        for build in self.builds:
            koji.tagBuild(tag, build.nvr, force=True)
        return koji.multiCall()

    def remove_tag(self, tag, koji=None):
        """ Remove a koji tag from all builds in this update """
        log.debug('Removing tag %s from %s' % (tag, self.title))
        if not tag:
            log.warn("Not removing builds of %s from empty tag" % self.title)
            return []  # An empty iterator in place of koji multicall

        return_multicall = not koji
        if not koji:
            koji = buildsys.get_session()
            koji.multicall = True
        for build in self.builds:
            koji.untagBuild(tag, build.nvr, force=True)
        if return_multicall:
            return koji.multiCall()

    def request_complete(self):
        """Perform post-request actions"""
        now = datetime.utcnow()
        if self.request is UpdateRequest.testing:
            self.status = UpdateStatus.testing
            self.date_testing = now
        elif self.request is UpdateRequest.stable:
            self.status = UpdateStatus.stable
            self.date_stable = now
        self.request = None
        self.date_pushed = now
        self.pushed = True

    def modify_bugs(self):
        """ Comment on and close this updates bugs as necessary

        This typically gets called by the Masher at the end.
        """
        if self.status is UpdateStatus.testing:
            for bug in self.bugs:
                log.debug('Adding testing comment to bugs for %s', self.title)
                bug.testing(self)
        elif self.status is UpdateStatus.stable:
            if not self.close_bugs:
                for bug in self.bugs:
                    log.debug('Adding stable comment to bugs for %s', self.title)
                    bug.add_comment(self)
            else:
                if self.type is UpdateType.security:
                    # Only close the tracking bugs
                    # https://github.com/fedora-infra/bodhi/issues/368#issuecomment-135155215
                    for bug in self.bugs:
                        if not bug.parent:
                            log.debug("Closing tracker bug %d" % bug.bug_id)
                            bug.close_bug(self)
                else:
                    for bug in self.bugs:
                        bug.close_bug(self)

    def status_comment(self, db):
        """
        Add a comment to this update about a change in status
        """
        if self.status is UpdateStatus.stable:
            self.comment(db, u'This update has been pushed to stable.',
                         author=u'bodhi')
        elif self.status is UpdateStatus.testing:
            self.comment(db, u'This update has been pushed to testing.',
                         author=u'bodhi')
        elif self.status is UpdateStatus.obsolete:
            self.comment(db, u'This update has been obsoleted.', author=u'bodhi')

    def send_update_notice(self):
        log.debug("Sending update notice for %s" % self.title)
        mailinglist = None
        sender = config.get('bodhi_email')
        if not sender:
            log.error("bodhi_email not defined in configuration!  Unable " +
                      "to send update notice")
            return

        # eg: fedora_epel
        release_name = self.release.id_prefix.lower().replace('-', '_')
        if self.status is UpdateStatus.stable:
            mailinglist = config.get('%s_announce_list' % release_name)
        elif self.status is UpdateStatus.testing:
            mailinglist = config.get('%s_test_announce_list' % release_name)

        # switch email template to legacy if update aims to EPEL <= 7
        if release_name == 'fedora_epel' and self.release.version_int <= 7:
            templatetype = '%s_legacy_errata_template' % release_name
        else:
            templatetype = '%s_errata_template' % release_name

        if mailinglist:
            for subject, body in mail.get_template(self, templatetype):
                mail.send_mail(sender, mailinglist, subject, body)
                notifications.publish(
                    topic='errata.publish',
                    msg=dict(subject=subject, body=body, update=self))
        else:
            log.error("Cannot find mailing list address for update notice")
            log.error("release_name = %r", release_name)

    def get_url(self):
        """ Return the relative URL to this update """
        path = ['updates']
        if self.alias:
            path.append(self.alias)
        else:
            path.append(quote(self.title))
        return os.path.join(*path)

    def abs_url(self, request=None):
        """ Return the absolute URL to this update """
        base = config['base_address']
        return os.path.join(base, self.get_url())

    url = abs_url

    def __str__(self):
        """
        Return a string representation of this update.
        """
        val = u"%s\n%s\n%s\n" % ('=' * 80, u'\n'.join(wrap(
            self.title.replace(',', ', '), width=80, initial_indent=' ' * 5,
            subsequent_indent=' ' * 5)), '=' * 80)
        if self.alias:
            val += u"  Update ID: %s\n" % self.alias
        val += u"""    Release: %s
     Status: %s
       Type: %s
      Karma: %d""" % (self.release.long_name, self.status.description,
                      self.type.description, self.karma)
        if self.critpath:
            val += u"\n   Critpath: %s" % self.critpath
        if self.request is not None:
            val += u"\n    Request: %s" % self.request.description
        if len(self.bugs):
            bugs = self.get_bugstring(show_titles=True)
            val += u"\n       Bugs: %s" % bugs
        if len(self.cves):
            val += u"\n       CVEs: %s" % self.get_cvestring()
        if self.notes:
            notes = wrap(
                self.notes, width=67, subsequent_indent=' ' * 11 + ': ')
            val += u"\n      Notes: %s" % '\n'.join(notes)
        username = None
        if self.user:
            username = self.user.name
        val += u"""
  Submitter: %s
  Submitted: %s\n""" % (username, self.date_submitted)
        if len(self.comments):
            val += u"   Comments: "
            comments = []
            for comment in self.comments:
                if comment.anonymous:
                    anonymous = " (unauthenticated)"
                else:
                    anonymous = ""
                comments.append(u"%s%s%s - %s (karma %s)" % (' ' * 13,
                                comment.user.name, anonymous, comment.timestamp,
                                comment.karma))
                if comment.text:
                    text = wrap(comment.text, initial_indent=' ' * 13,
                                subsequent_indent=' ' * 13, width=67)
                    comments.append(u'\n'.join(text))
            val += u'\n'.join(comments).lstrip() + u'\n'
        val += u"\n  %s\n" % self.abs_url()
        return val

    def update_bugs(self, bug_ids, session):
        """
        Create any new bugs, and remove any missing ones. Destroy removed bugs
        that are no longer referenced anymore.

        :returns: a list of new Bug instances.
        """
        to_remove = [bug for bug in self.bugs if bug.bug_id not in bug_ids]

        for bug in to_remove:
            self.bugs.remove(bug)
            if len(bug.updates) == 0:
                # Don't delete the Bug instance if there is any associated BugKarma
                if not session.query(BugKarma).filter_by(bug_id=bug.bug_id).count():
                    log.debug("Destroying stray Bugzilla #%d" % bug.bug_id)
                    session.delete(bug)
        session.flush()

        new = []
        for bug_id in bug_ids:
            bug = Bug.get(int(bug_id), session)
            if not bug:
                bug = Bug(bug_id=int(bug_id))
                session.add(bug)
                session.flush()
            if bug not in self.bugs:
                self.bugs.append(bug)
                new.append(bug.bug_id)
            if bug.security and self.type != UpdateType.security:
                self.type = UpdateType.security

        session.flush()
        return new

    def update_cves(self, cves, session):
        """
        Create any new CVES, and remove any missing ones.  Destroy removed CVES
        that are no longer referenced anymore.
        """
        for cve in self.cves:
            if cve.cve_id not in cves and len(cve.updates) == 0:
                log.debug("Destroying stray CVE #%s" % cve.cve_id)
                session.delete(cve)
        for cve_id in cves:
            cve = CVE.query.filter_by(cve_id=cve_id).one()
            if cve not in self.cves:
                self.cves.append(cve)
                log.debug("Creating new CVE: %s" % cve_id)
                cve = CVE(cve_id=cve_id)
                session.save(cve)
                self.cves.append(cve)
        session.flush()

    def obsolete_if_unstable(self, db):
        """
        If an update with pending status(autopush enabled) reaches unstable karma
        threshold, make sure it gets obsoleted.
        """
        if self.autokarma and self.status is UpdateStatus.pending \
                and self.request is UpdateRequest.testing and self.unstable_karma not in (0, None) \
                and self.karma <= self.unstable_karma:
            log.info("%s has reached unstable karma thresholds" % self.title)
            self.obsolete(db)
            flash_log("%s has been obsoleted." % self.title)
        return

    def comment(self, session, text, karma=0, author=None, anonymous=False,
                karma_critpath=0, bug_feedback=None, testcase_feedback=None,
                check_karma=True):
        """Add a comment to this update.

        If the karma reaches the 'stable_karma' value, then request that this update be marked
        as stable.  If it reaches the 'unstable_karma', it is unpushed.
        """
        if not author:
            raise ValueError('You must provide a comment author')

        caveats = []

        # Listify these
        bug_feedback = bug_feedback or []
        testcase_feedback = testcase_feedback or []

        if self.user.name == author:
            if karma != 0:
                karma = 0
                notice = 'You may not give karma to your own updates.'
                caveats.append({'name': 'karma', 'description': notice})

        comment = Comment(
            text=text, anonymous=anonymous,
            karma=karma, karma_critpath=karma_critpath)
        session.add(comment)

        if anonymous:
            author = u'anonymous'
        try:
            user = session.query(User).filter_by(name=author).one()
        except NoResultFound:
            user = User(name=author)
            session.add(user)

        user.comments.append(comment)
        self.comments.append(comment)
        session.flush()

        if not anonymous and karma != 0:
            # Determine whether this user has already left karma, and if so what the most recent
            # karma value they left was. We should examine all but the most recent comment, since
            # that is the comment we just added.
            previous_karma = None
            for c in reversed(self.comments[:-1]):
                if c.user.name == author and c.karma:
                    previous_karma = c.karma
                    break
            if previous_karma and karma != previous_karma:
                caveats.append({
                    'name': 'karma',
                    'description': 'Your karma standing was reversed.',
                })
            else:
                log.debug('Ignoring duplicate %d karma from %s on %s' % (karma, author, self.title))

            log.info("Updated %s karma to %d" % (self.title, self.karma))

            if check_karma and author not in config.get('system_users'):
                try:
                    self.check_karma_thresholds(session, u'bodhi')
                except LockedUpdateException:
                    pass
                except BodhiException as e:
                    # This gets thrown if the karma is pushed over the
                    # threshold, but it is a critpath update that is not
                    # critpath_approved. ... among other cases.
                    log.exception('Problem checking the karma threshold.')
                    caveats.append({
                        'name': 'karma', 'description': str(e),
                    })

            # Obsolete pending update if it reaches unstable karma threshold
            self.obsolete_if_unstable(session)

        session.flush()

        for feedback_dict in bug_feedback:
            feedback = BugKarma(**feedback_dict)
            session.add(feedback)
            comment.bug_feedback.append(feedback)

        for feedback_dict in testcase_feedback:
            feedback = TestCaseKarma(**feedback_dict)
            session.add(feedback)
            comment.testcase_feedback.append(feedback)

        session.flush()

        # Publish to fedmsg
        if author not in config.get('system_users'):
            notifications.publish(topic='update.comment', msg=dict(
                comment=comment.__json__(anonymize=True),
                agent=author,
            ))

        # Send a notification to everyone that has commented on this update
        people = set()
        for person in self.get_maintainers():
            if person.email:
                people.add(person.email)
            else:
                people.add(person.name)
        for comment in self.comments:
            if comment.anonymous or comment.user.name == u'bodhi':
                continue
            if comment.user.email:
                people.add(comment.user.email)
            else:
                people.add(comment.user.name)
        mail.send(people, 'comment', self, sender=None, agent=author)
        return comment, caveats

    def unpush(self, db):
        """ Move this update back to its dist-fX-updates-candidate tag """
        log.debug("Unpushing %s" % self.title)
        koji = buildsys.get_session()

        if self.status is UpdateStatus.unpushed:
            log.debug("%s already unpushed" % self.title)
            return

        if self.status is not UpdateStatus.testing:
            raise BodhiException("Can't unpush a %s update"
                                 % self.status.description)

        self.untag(db)

        for build in self.builds:
            koji.tagBuild(self.release.candidate_tag, build.nvr, force=True)

        self.pushed = False
        self.status = UpdateStatus.unpushed
        self.request = None

    def revoke(self):
        """ Remove pending request for this update """
        log.debug("Revoking %s" % self.title)

        if not self.request:
            raise BodhiException(
                "Can only revoke an update with an existing request")

        if self.status not in [UpdateStatus.pending, UpdateStatus.testing,
                               UpdateStatus.obsolete, UpdateStatus.unpushed]:
            raise BodhiException(
                "Can only revoke a pending, testing, unpushed, or obsolete "
                "update, not one that is %s" % self.status.description)

        # Remove the 'pending' koji tags from this update so taskotron stops
        # evalulating them.
        if self.request is UpdateRequest.testing:
            self.remove_tag(self.release.pending_signing_tag)
            self.remove_tag(self.release.pending_testing_tag)
        elif self.request is UpdateRequest.stable:
            self.remove_tag(self.release.pending_stable_tag)

        self.request = None

    def untag(self, db):
        """ Untag all of the builds in this update """
        log.info("Untagging %s" % self.title)
        koji = buildsys.get_session()
        tag_types, tag_rels = Release.get_tags(db)
        for build in self.builds:
            for tag in build.get_tags():
                # Only remove tags that we know about
                if tag in tag_rels:
                    koji.untagBuild(tag, build.nvr, force=True)
                else:
                    log.info("Skipping tag that we don't know about: %s" % tag)
        self.pushed = False

    def obsolete(self, db, newer=None):
        """
        Obsolete this update. Even though unpushing/obsoletion is an "instant"
        action, changes in the repository will not propagate until the next
        mash takes place.
        """
        log.debug("Obsoleting %s" % self.title)
        self.untag(db)
        self.status = UpdateStatus.obsolete
        self.request = None
        if newer:
            self.comment(db, u"This update has been obsoleted by [%s](%s)." % (
                newer.nvr, newer.update.abs_url()), author=u'bodhi')
        else:
            self.comment(db, u"This update has been obsoleted.", author=u'bodhi')

    def get_maintainers(self):
        """
        Return a list of User objects that have commit access to all of the
        packages that are contained within this update.
        """
        people = set([self.user])
        for build in self.builds:
            if build.package.committers:
                for committer in build.package.committers:
                    people.add(committer)
        return list(people)

    @property
    def product_version(self):
        """
        Return a string of the product version that this update's release is associated with.

        The product version is a string, such as "fedora-26", and is used when querying Greenwave
        for test gating decisions.

        Returns:
            basestring: The product version associated with this Update's Release.
        """
        return self.release.long_name.lower().replace(' ', '-')

    def check_requirements(self, session, settings):
        """ Check that an update meets its self-prescribed policy to be pushed

        Returns a tuple containing (result, reason) where result is a boolean
        and reason is a string.
        """

        requirements = tokenize(self.requirements or '')
        requirements = list(requirements)

        if not requirements:
            return True, "No checks required."

        try:
            # https://github.com/fedora-infra/bodhi/issues/362
            since = self.last_modified.isoformat().rsplit('.', 1)[0]
        except Exception as e:
            log.exception("Failed to determine last_modified from %r : %r",
                          self.last_modified, str(e))
            return False, "Failed to determine last_modified: %r" % str(e)

        try:
            # query results for this update
            query = dict(type='bodhi_update', item=self.alias, since=since,
                         testcases=','.join(requirements))
            results = list(bodhi.server.util.taskotron_results(settings, **query))

            # query results for each build
            # retrieve timestamp for each build so that queries can be optimized
            koji = buildsys.get_session()
            koji.multicall = True
            for build in self.builds:
                koji.getBuild(build.nvr)
            buildinfos = koji.multiCall()

            for index, build in enumerate(self.builds):
                multicall_response = buildinfos[index]
                if (not isinstance(multicall_response, list) or
                        not isinstance(multicall_response[0], dict)):
                    msg = ("Error retrieving data from Koji for %r: %r" %
                           (build.nvr, multicall_response))
                    log.error(msg)
                    raise TypeError(msg)

                buildinfo = multicall_response[0]
                ts = datetime.utcfromtimestamp(buildinfo['completion_ts']).isoformat()

                query = dict(type='koji_build', item=build.nvr, since=ts,
                             testcases=','.join(requirements))
                build_results = list(bodhi.server.util.taskotron_results(settings, **query))
                results.extend(build_results)

        except Exception as e:
            log.exception("Failed retrieving requirements results: %r", str(e))
            return False, "Failed retrieving requirements results: %r" % str(e)

        for testcase in requirements:
            relevant = [result for result in results
                        if result['testcase']['name'] == testcase]

            if not relevant:
                return False, 'No result found for required testcase %s' % testcase

            by_arch = defaultdict(list)
            for result in relevant:
                arch = result['data'].get('arch', ['noarch'])[0]
                by_arch[arch].append(result)

            for arch, result in by_arch.items():
                latest = relevant[0]  # resultsdb results are ordered chronologically
                if latest['outcome'] not in ['PASSED', 'INFO']:
                    return False, "Required task %s returned %s" % (
                        latest['testcase']['name'], latest['outcome'])

        if config.get('test_gating.required') and not self.test_gating_passed:
            return (False, "Required tests did not pass on this update")

        # TODO - check require_bugs and require_testcases also?

        return True, "All checks pass."

    def check_karma_thresholds(self, db, agent):
        """
        Check if we have reached either karma threshold, call set_request if necessary,
        and Ignore karma thresholds if the update is locked and raise exception
        """
        # Raise Exception if the update is locked
        if self.locked:
            log.debug('%s locked. Ignoring karma thresholds.' % self.title)
            raise LockedUpdateException
        # Return if the status of the update is not in testing or pending
        if self.status not in (UpdateStatus.testing, UpdateStatus.pending):
            return
        # If an update receives negative karma disable autopush
        if self.autokarma and self._composite_karma[1] != 0 and self.status is \
                UpdateStatus.testing and self.request is not UpdateRequest.stable:
            log.info("Disabling Auto Push since the update has received negative karma")
            self.autokarma = False
            text = config.get('disable_automatic_push_to_stable')
            self.comment(db, text, author=u'bodhi')
        elif self.stable_karma and self.karma >= self.stable_karma:
            if self.autokarma:
                if self.severity is UpdateSeverity.urgent or self.type is UpdateType.newpackage:
                    log.info("Automatically marking %s as stable" % self.title)
                    self.set_request(db, UpdateRequest.stable, agent)
                else:
                    log.info("Automatically adding %s to batch of updates that will be pushed to"
                             " stable at a later date" % self.title)
                    self.set_request(db, UpdateRequest.batched, agent)

                self.date_pushed = None
                notifications.publish(
                    topic='update.karma.threshold.reach',
                    msg=dict(update=self, status='stable'))
            else:
                # Add the 'testing_approval_msg_based_on_karma' message now
                log.info((
                    "%s update has reached the stable karma threshold and can be pushed to "
                    "stable now if the maintainer wishes") % self.title)
        elif self.unstable_karma and self.karma <= self.unstable_karma:
            if self.status is UpdateStatus.pending and not self.autokarma:
                pass
            else:
                log.info("Automatically unpushing %s" % self.title)
                self.obsolete(db)
                notifications.publish(
                    topic='update.karma.threshold.reach',
                    msg=dict(update=self, status='unstable'))

    @property
    def builds_json(self):
        return json.dumps([build.nvr for build in self.builds])

    @property
    def requirements_json(self):
        return json.dumps(list(tokenize(self.requirements or '')))

    @property
    def last_modified(self):
        """ Return the last time this update was edited or created.

        This gets used specifically by taskotron/resultsdb queries so we only
        query for test runs that occur *after* the last time this update
        (in its current form) was in play.
        """

        # Prune out None values that have not been set
        possibilities = [self.date_submitted, self.date_modified]
        possibilities = [p for p in possibilities if p]

        if not possibilities:  # Should be un-possible.
            raise ValueError("Update has no timestamps set: %r" % self)

        possibilities.sort()  # Sort smallest to largest (oldest to newest)
        return possibilities[-1]  # Return the last one

    @property
    def critpath_approved(self):
        """ Return whether or not this critpath update has been approved """
        # https://fedorahosted.org/bodhi/ticket/642
        if self.meets_testing_requirements:
            return True
        release_name = self.release.name.lower().replace('-', '')
        status = config.get('%s.status' % release_name, None)
        if status:
            num_admin_approvals = config.get('%s.%s.critpath.num_admin_approvals' % (
                release_name, status), None)
            min_karma = config.get('%s.%s.critpath.min_karma' % (
                release_name, status), None)
            if num_admin_approvals is not None and min_karma:
                return self.num_admin_approvals >= int(num_admin_approvals) and \
                    self.karma >= int(min_karma)
        return self.num_admin_approvals >= config.get('critpath.num_admin_approvals') and \
            self.karma >= config.get('critpath.min_karma')

    @property
    def meets_testing_requirements(self):
        """
        Return whether or not this update meets the testing requirements
        for this specific release.

        If this release does not have a mandatory testing requirement, then
        simply return True.
        """
        num_days = self.mandatory_days_in_testing

        if config.get('test_gating.required') and not self.test_gating_passed:
            return False

        if self.critpath:
            # Ensure there is no negative karma. We're looking at the sum of
            # each users karma for this update, which takes into account
            # changed votes.
            if self._composite_karma[1] < 0:
                return False
            return self.days_in_testing >= num_days

        if not num_days:
            return True

        # non-autokarma updates have met the testing requirements if they've reached the karma
        # threshold.
        if not self.autokarma and self.stable_karma not in (0, None)\
                and self.karma >= self.stable_karma:
            return True

        # Any update that reaches num_days has met the testing requirements.
        return self.days_in_testing >= num_days

    @property
    def met_testing_requirements(self):
        """
        Return whether or not this update has already met the testing
        requirements and bodhi has commented on the update that the
        requirements have been met. This is used to determine whether bodhi
        should add the comment about the Update's eligibility to be pushed,
        as we only want Bodhi to add the comment once.

        If this release does not have a mandatory testing requirement, then
        simply return True.
        """
        min_num_days = self.mandatory_days_in_testing
        if min_num_days:
            if not self.meets_testing_requirements:
                return False
        else:
            return True
        for comment in self.comments_since_karma_reset:
            if comment.user.name == u'bodhi' and \
               comment.text.startswith('This update has reached') and \
               'and can be pushed to stable now if the ' \
               'maintainer wishes' in comment.text:
                return True
        return False

    @property
    def days_to_stable(self):
        """Return the number of days until an update can be pushed to stable.

        This method will return the number of days until an update can be pushed to stable, or 0.
        0 is returned if the update meets testing requirements already, if it doesn't have a
        "truthy" date_testing attribute, or if it's been in testing for the release's
        mandatory_days_in_testing or longer."""
        if not self.meets_testing_requirements and self.date_testing:
            num_days = (self.mandatory_days_in_testing - self.days_in_testing)
            if num_days > 0:
                return num_days
        return 0

    @property
    def days_in_testing(self):
        """ Return the number of days that this update has been in testing """
        if self.date_testing:
            return (datetime.utcnow() - self.date_testing).days
        else:
            return 0

    @property
    def num_admin_approvals(self):
        """ Return the number of Releng/QA approvals of this update """
        approvals = 0
        for comment in self.comments:
            if comment.karma != 1:
                continue
            admin_groups = config.get('admin_groups')
            for group in comment.user.groups:
                if group.name in admin_groups:
                    approvals += 1
                    break
        return approvals

    @property
    def test_cases(self):
        tests = set()
        for build in self.builds:
            for test in build.package.test_cases:
                tests.add(test.name)
        return sorted(list(tests))

    @property
    def full_test_cases(self):
        tests = set()
        for build in self.builds:
            test_names = set()
            for test in build.package.test_cases:
                if test.name not in test_names:
                    test_names.add(test.name)
                    tests.add(test)
        return sorted(list(tests))

    @property
    def requested_tag(self):
        """Return the tag that update has requested"""
        tag = None
        if self.request is UpdateRequest.stable:
            tag = self.release.stable_tag
            # [No Frozen Rawhide] Move stable builds going to a pending
            # release to the Release.dist-tag
            if self.release.state is ReleaseState.pending:
                tag = self.release.dist_tag
        elif self.request in (UpdateRequest.testing, UpdateRequest.batched):
            tag = self.release.testing_tag
        elif self.request is UpdateRequest.obsolete:
            tag = self.release.candidate_tag
        if not tag:
            log.error(
                'Unable to determine requested tag for %s.' % self.title)
        return tag

    def __json__(self, request=None, anonymize=False):
        result = super(Update, self).__json__(
            request=request, anonymize=anonymize)
        # Duplicate alias as updateid for backwards compat with bodhi1
        result['updateid'] = result['alias']
        # Also, put the update submitter's name in the same place we put
        # it for bodhi1 to make fedmsg.meta compat much more simple.
        result['submitter'] = result['user']['name']
        # Include the karma total in the results
        result['karma'] = self.karma
        # Also, the Update content_type (derived from the builds content_types)
        result['content_type'] = self.content_type.value if self.content_type else None

        # For https://github.com/fedora-infra/bodhi/issues/270, throw the JSON
        # of the test cases in our output as well but take extra care to
        # short-circuit some of the insane recursion for
        # https://github.com/fedora-infra/bodhi/issues/343
        seen = [Package, TestCaseKarma]
        result['test_cases'] = [
            test._to_json(
                obj=test,
                seen=seen,
                request=request,
                anonymize=anonymize)
            for test in self.full_test_cases
        ]

        return result


# Used for many-to-many relationships between karma and a bug
class BugKarma(Base):
    __tablename__ = 'comment_bug_assoc'

    karma = Column(Integer, default=0)

    comment_id = Column(Integer, ForeignKey('comments.id'))
    comment = relationship("Comment", backref='bug_feedback')

    bug_id = Column(Integer, ForeignKey('bugs.bug_id'))
    bug = relationship("Bug", backref='feedback')


# Used for many-to-many relationships between karma and a bug
class TestCaseKarma(Base):
    __tablename__ = 'comment_testcase_assoc'

    karma = Column(Integer, default=0)

    comment_id = Column(Integer, ForeignKey('comments.id'))
    comment = relationship("Comment", backref='testcase_feedback')

    testcase_id = Column(Integer, ForeignKey('testcases.id'))
    testcase = relationship("TestCase", backref='feedback')


class Comment(Base):
    __tablename__ = 'comments'
    __exclude_columns__ = tuple()
    __get_by__ = ('id',)
    # If 'anonymous' is true, then scrub the 'author' field in __json__(...)
    __anonymity_map__ = {'user': u'anonymous'}

    karma = Column(Integer, default=0)
    karma_critpath = Column(Integer, default=0)
    text = Column(UnicodeText, nullable=False)
    anonymous = Column(Boolean, default=False)
    timestamp = Column(DateTime, default=datetime.utcnow)

    update_id = Column(Integer, ForeignKey('updates.id'))
    user_id = Column(Integer, ForeignKey('users.id'))

    def url(self):
        url = '/updates/' + self.update.title + '#comment-' + str(self.id)
        return url

    @property
    def unique_testcase_feedback(self):
        """This will filter out duplicates for testcases. It will return the
        correct number of testcases in testcase_feedbacks as a list.
        """
        feedbacks = self.testcase_feedback
        unique_feedbacks = set()
        filtered_feedbacks = list()
        for feedback in feedbacks:
            if feedback.testcase.name not in unique_feedbacks:
                unique_feedbacks.add(feedback.testcase.name)
                filtered_feedbacks.append(feedback)

        return filtered_feedbacks

    def __json__(self, *args, **kwargs):
        result = super(Comment, self).__json__(*args, **kwargs)
        # Duplicate 'user' as 'author' just for backwards compat with bodhi1.
        # Things like fedmsg and fedbadges rely on this.
        if not self.anonymous and result['user']:
            result['author'] = result['user']['name']
        else:
            result['author'] = u'anonymous'

        # Similarly, duplicate the update's title as update_title.
        result['update_title'] = result['update']['title']

        # Updates used to have a karma column which would be included in result['update']. The
        # column was replaced with a property, so we need to include it here for backwards
        # compatibility.
        result['update']['karma'] = self.update.karma

        return result

    def __str__(self):
        karma = '0'
        if self.karma != 0:
            karma = '%+d' % (self.karma,)
        if self.anonymous:
            anonymous = " (unauthenticated)"
        else:
            anonymous = ""
        return "%s%s - %s (karma: %s)\n%s" % (self.user.name, anonymous,
                                              self.timestamp, karma, self.text)


class CVE(Base):
    __tablename__ = 'cves'
    __exclude_columns__ = ('id', 'updates', 'bugs')
    __get_by__ = ('cve_id',)

    cve_id = Column(Unicode(13), unique=True, nullable=False)

    @property
    def url(self):
        return "http://www.cve.mitre.org/cgi-bin/cvename.cgi?name=%s" % \
            self.cve_id


class Bug(Base):
    __tablename__ = 'bugs'
    __exclude_columns__ = ('id', 'cves', 'updates')
    __get_by__ = ('bug_id',)

    # Bug number. If None, assume ``url`` points to an external bug tracker
    bug_id = Column(Integer, unique=True)

    # The title of the bug
    title = Column(Unicode(255))

    # If we're dealing with a security bug
    security = Column(Boolean, default=False)

    # Bug URL.  If None, then assume it's in Red Hat Bugzilla
    url = Column('url', UnicodeText)

    # If this bug is a parent tracker bug for release-specific bugs
    parent = Column(Boolean, default=False)

    # List of Mitre CVE's associated with this bug
    cves = relationship(CVE, secondary=bug_cve_table, backref='bugs')

    @property
    def url(self):
        return config['buglink'] % self.bug_id

    def update_details(self, bug=None):
        """ Grab details from rhbz to populate our bug fields.

        This is typically called "offline" in the UpdatesHandler consumer.
        """
        bugs.bugtracker.update_details(bug, self)

    def default_message(self, update):
        message = config['stable_bug_msg'] % (
            update.get_title(delim=', '), "%s %s" % (
                update.release.long_name, update.status.description))
        if update.status is UpdateStatus.testing:
            template = config['testing_bug_msg']

            if update.release.id_prefix == "FEDORA-EPEL":
                if 'testing_bug_epel_msg' in config:
                    template = config['testing_bug_epel_msg']
                else:
                    log.warn("No 'testing_bug_epel_msg' found in the config.")

            message += template % (config.get('base_address') + update.get_url())
        return message

    def add_comment(self, update, comment=None):
        if (update.type is UpdateType.security and self.parent and
                update.status is not UpdateStatus.stable):
            log.debug('Not commenting on parent security bug %s', self.bug_id)
        else:
            if not comment:
                comment = self.default_message(update)
            log.debug("Adding comment to Bug #%d: %s" % (self.bug_id, comment))
            bugs.bugtracker.comment(self.bug_id, comment)

    def testing(self, update):
        """
        Change the status of this bug to ON_QA, and comment on the bug with
        some details on how to test and provide feedback for this update.
        """
        # Skip modifying Security Response bugs for testing updates
        if update.type is UpdateType.security and self.parent:
            log.debug('Not modifying on parent security bug %s', self.bug_id)
        else:
            comment = self.default_message(update)
            bugs.bugtracker.on_qa(self.bug_id, comment)

    def close_bug(self, update):
        # Build a mapping of package names to build versions
        # so that .close() can figure out which build version fixes which bug.
        versions = dict([
            (get_nvr(b.nvr)[0], b.nvr) for b in update.builds
        ])
        bugs.bugtracker.close(self.bug_id, versions=versions, comment=self.default_message(update))

    def modified(self, update):
        """ Change the status of this bug to MODIFIED """
        if update.type is UpdateType.security and self.parent:
            log.debug('Not modifying on parent security bug %s', self.bug_id)
        else:
            bugs.bugtracker.modified(self.bug_id)


user_group_table = Table('user_group_table', Base.metadata,
                         Column('user_id', Integer, ForeignKey('users.id')),
                         Column('group_id', Integer, ForeignKey('groups.id')))

stack_group_table = Table('stack_group_table', Base.metadata,
                          Column('stack_id', Integer, ForeignKey('stacks.id')),
                          Column('group_id', Integer, ForeignKey('groups.id')))

stack_user_table = Table('stack_user_table', Base.metadata,
                         Column('stack_id', Integer, ForeignKey('stacks.id')),
                         Column('user_id', Integer, ForeignKey('users.id')))


class User(Base):
    __tablename__ = 'users'
    __exclude_columns__ = ('comments', 'updates', 'packages', 'stacks',
                           'buildroot_overrides')
    __include_extras__ = ('avatar', 'openid')
    __get_by__ = ('name',)

    name = Column(Unicode(64), unique=True, nullable=False)
    email = Column(UnicodeText, unique=True)

    # A preference
    show_popups = Column(Boolean, default=True, server_default=text('TRUE'))

    # One-to-many relationships
    comments = relationship(Comment, backref=backref('user'), lazy='dynamic')
    updates = relationship(Update, backref=backref('user'), lazy='dynamic')

    # Many-to-many relationships
    groups = relationship("Group", secondary=user_group_table, backref='users')

    def avatar(self, request):
        if not request:
            return None
        context = dict(request=request)
        return get_avatar(context=context, username=self.name, size=24)

    def openid(self, request):
        if not request:
            return None
        template = request.registry.settings.get('openid_template')
        return template.format(username=self.name)


class Group(Base):
    __tablename__ = 'groups'
    __get_by__ = ('name',)
    __exclude_columns__ = ('id', 'stacks',)

    name = Column(Unicode(64), unique=True, nullable=False)

    # users backref


class BuildrootOverride(Base):
    """
    This model represents a Koji buildroot override.

    A buildroot override is a way to add a build to the Koji buildroot (the set of packages used
    to build a package) manually. Normally, builds are only available after they are pushed to
    "stable", but this allows a build to be added to the buildroot immediately. This is useful
    for updates that depend on each other to be built.

    Attributes:
        notes (unicode): A text field that holds arbitrary notes about the buildroot override.
        submission_date (DateTime): The date that the buildroot override was submitted.
        expiration_date (DateTime): The date that the buildroot override expires.
        expired_date (DateTime): The date that the buildroot override expired.
        build_id (int): The primary key of the :class:`Build` this override is related to.
        build (Build): The :class:`Build` object this override is related to.
        submitter_id (int): The primary key of the :class:`User` this override was created by.
        submitter (User): The :class:`User` this override was created by.
    """
    __tablename__ = 'buildroot_overrides'
    __include_extras__ = ('nvr',)
    __get_by__ = ('build_id',)

    build_id = Column(Integer, ForeignKey('builds.id'), nullable=False)
    submitter_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    notes = Column(Unicode, nullable=False)

    submission_date = Column(DateTime, default=datetime.utcnow, nullable=False)
    expiration_date = Column(DateTime, nullable=False)
    expired_date = Column(DateTime)

    build = relationship('Build', lazy='joined',
                         backref=backref('override', lazy='joined',
                                         uselist=False))
    submitter = relationship('User', lazy='joined',
                             backref=backref('buildroot_overrides',
                                             lazy='joined'))

    @property
    def nvr(self):
        """ Convenience access to the build NVR. """
        return self.build.nvr

    @classmethod
    def new(cls, request, **data):
        """Create a new buildroot override"""
        db = request.db

        build = data['build']

        if build.override is not None:
            request.errors.add('body', 'nvr',
                               '%s is already in a override' % build.nvr)
            return

        old_build = db.query(Build).filter(
            and_(
                Build.package_id == build.package_id,
                Build.release_id == build.release_id)).first()

        if old_build is not None and old_build.override is not None:
            # There already is a buildroot override for an older build of this
            # package in this release. Expire it
            old_build.override.expire()
            db.add(old_build.override)

        override = cls(**data)
        override.enable()
        db.add(override)
        db.flush()

        return override

    @classmethod
    def edit(cls, request, **data):
        """Edit an existing buildroot override"""
        db = request.db

        edited = data.pop('edited')
        override = cls.get(edited.id, db)

        if override is None:
            request.errors.add('body', 'edited',
                               'No buildroot override for this build')
            return

        override.submitter = data['submitter']
        override.notes = data['notes']
        override.expiration_date = data['expiration_date']

        now = datetime.utcnow()

        if override.expired_date is not None and override.expiration_date > now:
            # Buildroot override had expired, we need to unexpire it
            override.enable()

        elif data['expired']:
            override.expire()

        db.add(override)
        db.flush()

        return override

    def enable(self):
        koji_session = buildsys.get_session()
        koji_session.tagBuild(self.build.release.override_tag, self.build.nvr)

        notifications.publish(
            topic='buildroot_override.tag',
            msg=dict(override=self),
        )

        self.expired_date = None

    def expire(self):
        if self.expired_date is not None:
            return

        koji_session = buildsys.get_session()
        try:
            koji_session.untagBuild(self.build.release.override_tag,
                                    self.build.nvr, strict=True)
        except Exception, e:
            log.error('Unable to untag override %s: %s' % (self.build.nvr, e))
        self.expired_date = datetime.utcnow()

        notifications.publish(
            topic='buildroot_override.untag',
            msg=dict(override=self),
        )


class Stack(Base):
    """
    A Stack in bodhi represents a group of packages that are commonly pushed
    together as a group.
    """
    __tablename__ = 'stacks'
    __get_by__ = ('name',)

    name = Column(UnicodeText, unique=True, nullable=False)
    packages = relationship('Package', backref=backref('stack', lazy=True))
    description = Column(UnicodeText)
    requirements = Column(UnicodeText)

    # Many-to-many relationships
    groups = relationship("Group", secondary=stack_group_table, backref='stacks')
    users = relationship("User", secondary=stack_user_table, backref='stacks')
