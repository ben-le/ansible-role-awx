# Copyright (c) 2015 Ansible, Inc.
# All Rights Reserved.

# This file is a copy of the access.py file that existed in the 2.4 release of
# tower.  We're keeping it around for a little while in order to run
# before/after access validation during the 3.0 upgrade process. Once we're
# confident that this process is reliable, this file is no longer necessary
# and can be removed. - anoek 2/9/16

# Python
import os
import sys
import logging

# Django
from django.conf import settings
from django.db.models import F, Q
from django.contrib.auth.models import User

# Django REST Framework
from rest_framework.exceptions import ParseError, PermissionDenied

# AWX
from awx.main.utils import * # noqa
from awx.main.models import * # noqa
from awx.conf.license import LicenseForbids

__all__ = ['get_user_queryset', 'check_user_access']

PERM_INVENTORY_ADMIN = 'admin'
PERM_INVENTORY_READ = 'read'
PERM_INVENTORY_WRITE = 'write'
PERM_JOBTEMPLATE_CREATE = 'create'

PERMISSION_TYPES = [
    PERM_INVENTORY_ADMIN,
    PERM_INVENTORY_READ,
    PERM_INVENTORY_WRITE,
    PERM_INVENTORY_DEPLOY,
    PERM_INVENTORY_CHECK,
]

PERMISSION_TYPES_ALLOWING_INVENTORY_READ = [
    PERM_INVENTORY_ADMIN,
    PERM_INVENTORY_WRITE,
    PERM_INVENTORY_READ,
]

PERMISSION_TYPES_ALLOWING_INVENTORY_WRITE = [
    PERM_INVENTORY_ADMIN,
    PERM_INVENTORY_WRITE,
]

PERMISSION_TYPES_ALLOWING_INVENTORY_ADMIN = [
    PERM_INVENTORY_ADMIN,
]

logger = logging.getLogger('awx.main.access')

access_registry = {
    # <model_class>: [<access_class>, ...],
    # ...
}


def register_access(model_class, access_class):
    access_classes = access_registry.setdefault(model_class, [])
    access_classes.append(access_class)


def get_user_queryset(user, model_class):
    '''
    Return a queryset for the given model_class containing only the instances
    that should be visible to the given user.
    '''
    querysets = []
    for access_class in access_registry.get(model_class, []):
        access_instance = access_class(user)
        querysets.append(access_instance.get_queryset())
    if not querysets:
        return model_class.objects.none()
    elif len(querysets) == 1:
        return querysets[0]
    else:
        queryset = model_class.objects.all()
        for qs in querysets:
            queryset = queryset.filter(pk__in=qs.values_list('pk', flat=True))
        return queryset


def check_user_access(user, model_class, action, *args, **kwargs):
    '''
    Return True if user can perform action against model_class with the
    provided parameters.
    '''
    for access_class in access_registry.get(model_class, []):
        access_instance = access_class(user)
        access_method = getattr(access_instance, 'can_%s' % action, None)
        if not access_method:
            logger.debug('%s.%s not found', access_instance.__class__.__name__,
                         'can_%s' % action)
            continue
        result = access_method(*args, **kwargs)
        logger.debug('%s.%s %r returned %r', access_instance.__class__.__name__,
                     access_method.__name__, args, result)
        if result:
            return result
    return False


class BaseAccess(object):
    '''
    Base class for checking user access to a given model.  Subclasses should
    define the model attribute, override the get_queryset method to return only
    the instances the user should be able to view, and override/define can_*
    methods to verify a user's permission to perform a particular action.
    '''

    model = None

    def __init__(self, user):
        self.user = user

    def get_queryset(self):
        if self.user.is_superuser:
            return self.model.objects.all()
        else:
            return self.model.objects.none()

    def can_read(self, obj):
        return bool(obj and self.get_queryset().filter(pk=obj.pk).exists())

    def can_add(self, data):
        return self.user.is_superuser

    def can_change(self, obj, data):
        return self.user.is_superuser

    def can_write(self, obj, data):
        # Alias for change.
        return self.can_change(obj, data)

    def can_admin(self, obj, data):
        # Alias for can_change.  Can be overridden if admin vs. user change
        # permissions need to be different.
        return self.can_change(obj, data)

    def can_delete(self, obj):
        return self.user.is_superuser

    def can_attach(self, obj, sub_obj, relationship, data,
                   skip_sub_obj_read_check=False):
        if skip_sub_obj_read_check:
            return self.can_change(obj, None)
        else:
            return bool(self.can_change(obj, None) and
                        check_user_access(self.user, type(sub_obj), 'read', sub_obj))

    def can_unattach(self, obj, sub_obj, relationship):
        return self.can_change(obj, None)

    def check_license(self, add_host=False, feature=None, check_expiration=True):
        from awx.main.task_engine import TaskEnhancer
        validation_info = TaskEnhancer().validate_enhancements()
        if ('test' in sys.argv or 'py.test' in sys.argv[0] or 'jenkins' in sys.argv) and not os.environ.get('SKIP_LICENSE_FIXUP_FOR_TEST', ''):
            validation_info['free_instances'] = 99999999
            validation_info['time_remaining'] = 99999999
            validation_info['grace_period_remaining'] = 99999999

        if check_expiration and validation_info.get('time_remaining', None) is None:
            raise PermissionDenied("license is missing")
        if check_expiration and validation_info.get("grace_period_remaining") <= 0:
            raise PermissionDenied("license has expired")

        free_instances = validation_info.get('free_instances', 0)
        available_instances = validation_info.get('available_instances', 0)
        if add_host and free_instances == 0:
            raise PermissionDenied("license count of %s instances has been reached" % available_instances)
        elif add_host and free_instances < 0:
            raise PermissionDenied("license count of %s instances has been exceeded" % available_instances)
        elif not add_host and free_instances < 0:
            raise PermissionDenied("host count exceeds available instances")

        if feature is not None:
            if "features" in validation_info and not validation_info["features"].get(feature, False):
                raise LicenseForbids("Feature %s is not enabled in the active license" % feature)
            elif "features" not in validation_info:
                raise LicenseForbids("Features not found in active license")


class UserAccess(BaseAccess):
    '''
    I can see user records when:
     - I'm a superuser.
     - I'm that user.
     - I'm an org admin (org admins should be able to see all users, in order
       to add those users to the org).
     - I'm in an org with that user.
     - I'm on a team with that user.
    I can change some fields for a user (mainly password) when I am that user.
    I can change all fields for a user (admin access) or delete when:
     - I'm a superuser.
     - I'm their org admin.
    '''

    model = User

    def get_queryset(self):
        qs = self.model.objects.distinct()
        if self.user.is_superuser:
            return qs
        if settings.ORG_ADMINS_CAN_SEE_ALL_USERS and self.user.deprecated_admin_of_organizations.all().exists():
            return qs
        return qs.filter(
            Q(pk=self.user.pk) |
            Q(deprecated_organizations__in=self.user.deprecated_admin_of_organizations.all()) |
            Q(deprecated_organizations__in=self.user.deprecated_organizations.all()) |
            Q(deprecated_teams__in=self.user.deprecated_teams.all())
        ).distinct()

    def can_add(self, data):
        if data is not None and 'is_superuser' in data:
            if to_python_boolean(data['is_superuser'], allow_none=True) and not self.user.is_superuser:
                return False
        return bool(self.user.is_superuser or
                    self.user.deprecated_admin_of_organizations.exists())

    def can_change(self, obj, data):
        if data is not None and 'is_superuser' in data:
            if to_python_boolean(data['is_superuser'], allow_none=True) and not self.user.is_superuser:
                return False
        # A user can be changed if they are themselves, or by org admins or
        # superusers.  Change permission implies changing only certain fields
        # that a user should be able to edit for themselves.
        return bool(self.user == obj or self.can_admin(obj, data))

    def can_admin(self, obj, data):
        # Admin implies changing all user fields.
        if self.user.is_superuser:
            return True
        return bool(obj.deprecated_organizations.filter(deprecated_admins__in=[self.user]).exists())

    def can_delete(self, obj):
        if obj == self.user:
            # cannot delete yourself
            return False
        super_users = User.objects.filter(is_superuser=True)
        if obj.is_superuser and super_users.count() == 1:
            # cannot delete the last active superuser
            return False
        return bool(self.user.is_superuser or
                    obj.deprecated_organizations.filter(deprecated_admins__in=[self.user]).exists())


class OrganizationAccess(BaseAccess):
    '''
    I can see organizations when:
     - I am a superuser.
     - I am an admin or user in that organization.
    I can change or delete organizations when:
     - I am a superuser.
     - I'm an admin of that organization.
    '''

    model = Organization

    def get_queryset(self):
        qs = self.model.objects.distinct()
        qs = qs.select_related('created_by', 'modified_by')
        if self.user.is_superuser:
            return qs
        return qs.filter(Q(deprecated_admins__in=[self.user]) | Q(deprecated_users__in=[self.user]))

    def can_change(self, obj, data):
        return bool(self.user.is_superuser or
                    self.user in obj.deprecated_admins.all())

    def can_delete(self, obj):
        self.check_license(feature='multiple_organizations', check_expiration=False)
        return self.can_change(obj, None)


class InventoryAccess(BaseAccess):
    '''
    I can see inventory when:
     - I'm a superuser.
     - I'm an org admin of the inventory's org.
     - I have read, write or admin permissions on it.
    I can change inventory when:
     - I'm a superuser.
     - I'm an org admin of the inventory's org.
     - I have write or admin permissions on it.
    I can delete inventory when:
     - I'm a superuser.
     - I'm an org admin of the inventory's org.
     - I have admin permissions on it.
    I can run ad hoc commands when:
     - I'm a superuser.
     - I'm an org admin of the inventory's org.
     - I have read/write/admin permission on an inventory with the run_ad_hoc_commands flag set.
    '''

    model = Inventory

    def get_queryset(self, allowed=None, ad_hoc=None):
        allowed = allowed or PERMISSION_TYPES_ALLOWING_INVENTORY_READ
        qs = Inventory.objects.distinct()
        qs = qs.select_related('created_by', 'modified_by', 'organization')
        if self.user.is_superuser:
            return qs
        admin_of = qs.filter(organization__deprecated_admins__in=[self.user]).distinct()
        has_user_kw = dict(
            permissions__user__in=[self.user],
            permissions__permission_type__in=allowed,
        )
        if ad_hoc is not None:
            has_user_kw['permissions__run_ad_hoc_commands'] = ad_hoc
        has_user_perms = qs.filter(**has_user_kw).distinct()
        has_team_kw = dict(
            permissions__team__deprecated_users__in=[self.user],
            permissions__permission_type__in=allowed,
        )
        if ad_hoc is not None:
            has_team_kw['permissions__run_ad_hoc_commands'] = ad_hoc
        has_team_perms = qs.filter(**has_team_kw).distinct()
        return admin_of | has_user_perms | has_team_perms

    def has_permission_types(self, obj, allowed, ad_hoc=None):
        return bool(obj and self.get_queryset(allowed, ad_hoc).filter(pk=obj.pk).exists())

    def can_read(self, obj):
        return self.has_permission_types(obj, PERMISSION_TYPES_ALLOWING_INVENTORY_READ)

    def can_add(self, data):
        # If no data is specified, just checking for generic add permission?
        if not data:
            return bool(self.user.is_superuser or
                        self.user.deprecated_admin_of_organizations.exists())
        # Otherwise, verify that the user has access to change the parent
        # organization of this inventory.
        if self.user.is_superuser:
            return True
        else:
            org_pk = get_pk_from_dict(data, 'organization')
            org = get_object_or_400(Organization, pk=org_pk)
            if check_user_access(self.user, Organization, 'change', org, None):
                return True
        return False

    def can_change(self, obj, data):
        # Verify that the user has access to the new organization if moving an
        # inventory to a new organization.
        org_pk = get_pk_from_dict(data, 'organization')
        if obj and org_pk and obj.organization.pk != org_pk:
            org = get_object_or_400(Organization, pk=org_pk)
            if not check_user_access(self.user, Organization, 'change', org, None):
                return False
        # Otherwise, just check for write permission.
        return self.has_permission_types(obj, PERMISSION_TYPES_ALLOWING_INVENTORY_WRITE)

    def can_admin(self, obj, data):
        # Verify that the user has access to the new organization if moving an
        # inventory to a new organization.
        org_pk = get_pk_from_dict(data, 'organization')
        if obj and org_pk and obj.organization.pk != org_pk:
            org = get_object_or_400(Organization, pk=org_pk)
            if not check_user_access(self.user, Organization, 'change', org, None):
                return False
        # Otherwise, just check for admin permission.
        return self.has_permission_types(obj, PERMISSION_TYPES_ALLOWING_INVENTORY_ADMIN)

    def can_delete(self, obj):
        return self.can_admin(obj, None)

    def can_run_ad_hoc_commands(self, obj):
        return self.has_permission_types(obj, PERMISSION_TYPES_ALLOWING_INVENTORY_READ, True)


class HostAccess(BaseAccess):
    '''
    I can see hosts whenever I can see their inventory.
    I can change or delete hosts whenver I can change their inventory.
    '''

    model = Host

    def get_queryset(self):
        qs = self.model.objects.distinct()
        qs = qs.select_related('created_by', 'modified_by', 'inventory',
                               'last_job__job_template',
                               'last_job_host_summary__job')
        qs = qs.prefetch_related('groups')
        inventory_ids = set(self.user.get_queryset(Inventory).values_list('id', flat=True))
        return qs.filter(inventory_id__in=inventory_ids)

    def can_read(self, obj):
        return obj and check_user_access(self.user, Inventory, 'read', obj.inventory)

    def can_add(self, data):
        if not data or 'inventory' not in data:
            return False

        # Checks for admin or change permission on inventory.
        inventory_pk = get_pk_from_dict(data, 'inventory')
        inventory = get_object_or_400(Inventory, pk=inventory_pk)
        if not check_user_access(self.user, Inventory, 'change', inventory, None):
            return False

        # Check to see if we have enough licenses
        self.check_license(add_host=True)
        return True

    def can_change(self, obj, data):
        # Prevent moving a host to a different inventory.
        inventory_pk = get_pk_from_dict(data, 'inventory')
        if obj and inventory_pk and obj.inventory.pk != inventory_pk:
            raise PermissionDenied('Unable to change inventory on a host')
        # Checks for admin or change permission on inventory, controls whether
        # the user can edit variable data.
        return obj and check_user_access(self.user, Inventory, 'change', obj.inventory, None)

    def can_attach(self, obj, sub_obj, relationship, data,
                   skip_sub_obj_read_check=False):
        if not super(HostAccess, self).can_attach(obj, sub_obj, relationship,
                                                  data, skip_sub_obj_read_check):
            return False
        # Prevent assignments between different inventories.
        if obj.inventory != sub_obj.inventory:
            raise ParseError('Cannot associate two items from different inventories')
        return True

    def can_delete(self, obj):
        return obj and check_user_access(self.user, Inventory, 'delete', obj.inventory)


class GroupAccess(BaseAccess):
    '''
    I can see groups whenever I can see their inventory.
    I can change or delete groups whenever I can change their inventory.
    '''

    model = Group

    def get_queryset(self):
        qs = self.model.objects.distinct()
        qs = qs.select_related('created_by', 'modified_by', 'inventory')
        qs = qs.prefetch_related('parents', 'children', 'inventory_source')
        inventory_ids = set(self.user.get_queryset(Inventory).values_list('id', flat=True))
        return qs.filter(inventory_id__in=inventory_ids)

    def can_read(self, obj):
        return obj and check_user_access(self.user, Inventory, 'read', obj.inventory)

    def can_add(self, data):
        if not data or 'inventory' not in data:
            return False
        # Checks for admin or change permission on inventory.
        inventory_pk = get_pk_from_dict(data, 'inventory')
        inventory = get_object_or_400(Inventory, pk=inventory_pk)
        return check_user_access(self.user, Inventory, 'change', inventory, None)

    def can_change(self, obj, data):
        # Prevent moving a group to a different inventory.
        inventory_pk = get_pk_from_dict(data, 'inventory')
        if obj and inventory_pk and obj.inventory.pk != inventory_pk:
            raise PermissionDenied('Unable to change inventory on a group')
        # Checks for admin or change permission on inventory, controls whether
        # the user can attach subgroups or edit variable data.
        return obj and check_user_access(self.user, Inventory, 'change', obj.inventory, None)

    def can_attach(self, obj, sub_obj, relationship, data,
                   skip_sub_obj_read_check=False):
        if not super(GroupAccess, self).can_attach(obj, sub_obj, relationship,
                                                   data, skip_sub_obj_read_check):
            return False
        # Prevent assignments between different inventories.
        if obj.inventory != sub_obj.inventory:
            raise ParseError('Cannot associate two items from different inventories')
        # Prevent group from being assigned as its own (grand)child.
        if type(obj) == type(sub_obj):
            parent_pks = set(obj.all_parents.values_list('pk', flat=True))
            parent_pks.add(obj.pk)
            child_pks = set(sub_obj.all_children.values_list('pk', flat=True))
            child_pks.add(sub_obj.pk)
            if parent_pks & child_pks:
                return False
        return True

    def can_delete(self, obj):
        return obj and check_user_access(self.user, Inventory, 'delete', obj.inventory)


class InventorySourceAccess(BaseAccess):
    '''
    I can see inventory sources whenever I can see their group or inventory.
    I can change inventory sources whenever I can change their group.
    '''

    model = InventorySource

    def get_queryset(self):
        qs = self.model.objects.distinct()
        qs = qs.select_related('created_by', 'modified_by', 'group', 'inventory')
        inventory_ids = set(self.user.get_queryset(Inventory).values_list('id', flat=True))
        return qs.filter(Q(inventory_id__in=inventory_ids) |
                         Q(group__inventory_id__in=inventory_ids))

    def can_read(self, obj):
        if obj and obj.group:
            return check_user_access(self.user, Group, 'read', obj.group)
        elif obj and obj.inventory:
            return check_user_access(self.user, Inventory, 'read', obj.inventory)
        else:
            return False

    def can_add(self, data):
        # Automatically created from group or management command.
        return False

    def can_change(self, obj, data):
        # Checks for admin or change permission on group.
        if obj and obj.group:
            return check_user_access(self.user, Group, 'change', obj.group, None)
        # Can't change inventory sources attached to only the inventory, since
        # these are created automatically from the management command.
        else:
            return False

    def can_start(self, obj):
        return self.can_change(obj, {}) and obj.can_update


class InventoryUpdateAccess(BaseAccess):
    '''
    I can see inventory updates when I can see the inventory source.
    I can change inventory updates whenever I can change their source.
    I can delete when I can change/delete the inventory source.
    '''

    model = InventoryUpdate

    def get_queryset(self):
        qs = InventoryUpdate.objects.distinct()
        qs = qs.select_related('created_by', 'modified_by', 'inventory_source__group',
                               'inventory_source__inventory')
        inventory_sources_qs = self.user.get_queryset(InventorySource)
        return qs.filter(inventory_source__in=inventory_sources_qs)

    def can_cancel(self, obj):
        return self.can_change(obj, {}) and obj.can_cancel


class CredentialAccess(BaseAccess):
    '''
    I can see credentials when:
     - I'm a superuser.
     - It's a user credential and it's my credential.
     - It's a user credential and I'm an admin of an organization where that
       user is a member of admin of the organization.
     - It's a team credential and I'm an admin of the team's organization.
     - It's a team credential and I'm a member of the team.
    I can change/delete when:
     - I'm a superuser.
     - It's my user credential.
     - It's a user credential for a user in an org I admin.
     - It's a team credential for a team in an org I admin.
    '''

    model = Credential

    def get_queryset(self):
        """Return the queryset for credentials, based on what the user is
        permitted to see.
        """
        # Create a base queryset.
        # If the user is a superuser, and therefore can see everything, this
        # is also sufficient, and we are done.
        qs = self.model.objects.distinct()
        qs = qs.select_related('created_by', 'modified_by')
        if self.user.is_superuser:
            return qs

        # Get the list of organizations for which the user is an admin
        orgs_as_admin_ids = set(self.user.deprecated_admin_of_organizations.values_list('id', flat=True))
        return qs.filter(
            Q(deprecated_user=self.user) |
            Q(deprecated_user__deprecated_organizations__id__in=orgs_as_admin_ids) |
            Q(deprecated_user__deprecated_admin_of_organizations__id__in=orgs_as_admin_ids) |
            Q(deprecated_team__organization__id__in=orgs_as_admin_ids) |
            Q(deprecated_team__deprecated_users__in=[self.user])
        )

    def can_add(self, data):
        if self.user.is_superuser:
            return True
        user_pk = get_pk_from_dict(data, 'user')
        if user_pk:
            user_obj = get_object_or_400(User, pk=user_pk)
            return check_user_access(self.user, User, 'change', user_obj, None)
        team_pk = get_pk_from_dict(data, 'team')
        if team_pk:
            team_obj = get_object_or_400(Team, pk=team_pk)
            return check_user_access(self.user, Team, 'change', team_obj, None)
        return False

    def can_change(self, obj, data):
        if self.user.is_superuser:
            return True
        if not self.can_add(data):
            return False
        if self.user == obj.created_by:
            return True
        if obj.deprecated_user:
            if self.user == obj.deprecated_user:
                return True
            if obj.deprecated_user.deprecated_organizations.filter(deprecated_admins__in=[self.user]).exists():
                return True
            if obj.deprecated_user.deprecated_admin_of_organizations.filter(deprecated_admins__in=[self.user]).exists():
                return True
        if obj.deprecated_team:
            if self.user in obj.deprecated_team.organization.deprecated_admins.all():
                return True
        return False

    def can_delete(self, obj):
        # Unassociated credentials may be marked deleted by anyone, though we
        # shouldn't ever end up with those.
        if obj.deprecated_user is None and obj.deprecated_team is None:
            return True
        return self.can_change(obj, None)


class TeamAccess(BaseAccess):
    '''
    I can see a team when:
     - I'm a superuser.
     - I'm an admin of the team's organization.
     - I'm a member of that team.
    I can create/change a team when:
     - I'm a superuser.
     - I'm an org admin for the team's org.
    '''

    model = Team

    def get_queryset(self):
        qs = self.model.objects.distinct()
        qs = qs.select_related('created_by', 'modified_by', 'organization')
        if self.user.is_superuser:
            return qs
        return qs.filter(
            Q(organization__deprecated_admins__in=[self.user]) |
            Q(deprecated_users__in=[self.user])
        )

    def can_add(self, data):
        if self.user.is_superuser:
            return True
        else:
            org_pk = get_pk_from_dict(data, 'organization')
            org = get_object_or_400(Organization, pk=org_pk)
            if check_user_access(self.user, Organization, 'change', org, None):
                return True
        return False

    def can_change(self, obj, data):
        # Prevent moving a team to a different organization.
        org_pk = get_pk_from_dict(data, 'organization')
        if obj and org_pk and obj.organization.pk != org_pk:
            raise PermissionDenied('Unable to change organization on a team')
        if self.user.is_superuser:
            return True
        if obj.organization and self.user in obj.organization.deprecated_admins.all():
            return True
        return False

    def can_delete(self, obj):
        return self.can_change(obj, None)


class ProjectAccess(BaseAccess):
    '''
    I can see projects when:
     - I am a superuser.
     - I am an admin in an organization associated with the project.
     - I am a user in an organization associated with the project.
     - I am on a team associated with the project.
     - I have been explicitly granted permission to run/check jobs using the
       project.
     - I created the project but it isn't associated with an organization
    I can change/delete when:
     - I am a superuser.
     - I am an admin in an organization associated with the project.
     - I created the project but it isn't associated with an organization
    '''

    model = Project

    def get_queryset(self):
        qs = Project.objects.distinct()
        qs = qs.select_related('modified_by', 'credential', 'current_job', 'last_job')
        if self.user.is_superuser:
            return qs
        team_ids = Team.objects.filter(deprecated_users__in=[self.user])
        qs = qs.filter(Q(created_by=self.user, deprecated_organizations__isnull=True) |
                       Q(deprecated_organizations__deprecated_admins__in=[self.user]) |
                       Q(deprecated_organizations__deprecated_users__in=[self.user]) |
                       Q(deprecated_teams__in=team_ids))
        allowed_deploy = [PERM_JOBTEMPLATE_CREATE, PERM_INVENTORY_DEPLOY]
        allowed_check = [PERM_JOBTEMPLATE_CREATE, PERM_INVENTORY_DEPLOY, PERM_INVENTORY_CHECK]

        deploy_permissions = Permission.objects.filter(
            Q(user=self.user) | Q(team_id__in=team_ids),
            permission_type__in=allowed_deploy,
        )
        check_permissions = Permission.objects.filter(
            Q(user=self.user) | Q(team_id__in=team_ids),
            permission_type__in=allowed_check,
        )

        perm_deploy_qs = qs.filter(permissions__in=deploy_permissions)
        perm_check_qs = qs.filter(permissions__in=check_permissions)
        return qs | perm_deploy_qs | perm_check_qs

    def can_add(self, data):
        if self.user.is_superuser:
            return True
        if self.user.deprecated_admin_of_organizations.exists():
            return True
        return False

    def can_change(self, obj, data):
        if self.user.is_superuser:
            return True
        if obj.created_by == self.user and not obj.deprecated_organizations.count():
            return True
        if obj.deprecated_organizations.filter(deprecated_admins__in=[self.user]).exists():
            return True
        return False

    def can_delete(self, obj):
        return self.can_change(obj, None)

    def can_start(self, obj):
        return self.can_change(obj, {}) and obj.can_update


class ProjectUpdateAccess(BaseAccess):
    '''
    I can see project updates when I can see the project.
    I can change when I can change the project.
    I can delete when I can change/delete the project.
    '''

    model = ProjectUpdate

    def get_queryset(self):
        qs = ProjectUpdate.objects.distinct()
        qs = qs.select_related('created_by', 'modified_by', 'project')
        project_ids = set(self.user.get_queryset(Project).values_list('id', flat=True))
        return qs.filter(project_id__in=project_ids)

    def can_cancel(self, obj):
        return self.can_change(obj, {}) and obj.can_cancel

    def can_delete(self, obj):
        return obj and check_user_access(self.user, Project, 'delete', obj.project)


class PermissionAccess(BaseAccess):
    '''
    I can see a permission when:
     - I'm a superuser.
     - I'm an org admin and it's for a user in my org.
     - I'm an org admin and it's for a team in my org.
     - I'm a user and it's assigned to me.
     - I'm a member of a team and it's assigned to the team.
    I can create/change/delete when:
     - I'm a superuser.
     - I'm an org admin and the team/user is in my org and the inventory is in
       my org and the project is in my org.
    '''

    model = Permission

    def get_queryset(self):
        qs = self.model.objects.distinct()
        qs = qs.select_related('created_by', 'modified_by', 'user', 'team', 'inventory',
                               'project')
        if self.user.is_superuser:
            return qs
        orgs_as_admin_ids = set(self.user.deprecated_admin_of_organizations.values_list('id', flat=True))
        return qs.filter(
            Q(user__deprecated_organizations__in=orgs_as_admin_ids) |
            Q(user__deprecated_admin_of_organizations__in=orgs_as_admin_ids) |
            Q(team__organization__in=orgs_as_admin_ids) |
            Q(user=self.user) |
            Q(team__deprecated_users__in=[self.user])
        )

    def can_add(self, data):
        if not data:
            return True # generic add permission check
        user_pk = get_pk_from_dict(data, 'user')
        team_pk = get_pk_from_dict(data, 'team')
        if user_pk:
            user = get_object_or_400(User, pk=user_pk)
            if not check_user_access(self.user, User, 'admin', user, None):
                return False
        elif team_pk:
            team = get_object_or_400(Team, pk=team_pk)
            if not check_user_access(self.user, Team, 'admin', team, None):
                return False
        else:
            return False
        inventory_pk = get_pk_from_dict(data, 'inventory')
        if inventory_pk:
            inventory = get_object_or_400(Inventory, pk=inventory_pk)
            if not check_user_access(self.user, Inventory, 'admin', inventory, None):
                return False
        project_pk = get_pk_from_dict(data, 'project')
        if project_pk:
            project = get_object_or_400(Project, pk=project_pk)
            if not check_user_access(self.user, Project, 'admin', project, None):
                return False
        # FIXME: user/team, inventory and project should probably all be part
        # of the same organization.
        return True

    def can_change(self, obj, data):
        # Prevent assigning a permission to a different user.
        user_pk = get_pk_from_dict(data, 'user')
        if obj and user_pk and obj.user and obj.user.pk != user_pk:
            raise PermissionDenied('Unable to change user on a permission')
        # Prevent assigning a permission to a different team.
        team_pk = get_pk_from_dict(data, 'team')
        if obj and team_pk and obj.team and obj.team.pk != team_pk:
            raise PermissionDenied('Unable to change team on a permission')
        if self.user.is_superuser:
            return True
        # If changing inventory, verify access to the new inventory.
        new_inventory_pk = get_pk_from_dict(data, 'inventory')
        if obj and new_inventory_pk and obj.inventory and obj.inventory.pk != new_inventory_pk:
            inventory = get_object_or_400(Inventory, pk=new_inventory_pk)
            if not check_user_access(self.user, Inventory, 'admin', inventory, None):
                return False
        # If changing project, verify access to the new project.
        new_project = get_pk_from_dict(data, 'project')
        if obj and new_project and obj.project and obj.project.pk != new_project:
            project = get_object_or_400(Project, pk=new_project)
            if not check_user_access(self.user, Project, 'admin', project, None):
                return False
        # Check for admin access to the user or team.
        if obj.user and check_user_access(self.user, User, 'admin', obj.user, None):
            return True
        if obj.team and check_user_access(self.user, Team, 'admin', obj.team, None):
            return True
        return False

    def can_delete(self, obj):
        return self.can_change(obj, None)


class JobTemplateAccess(BaseAccess):
    '''
    I can see job templates when:
     - I am a superuser.
     - I can read the inventory, project and credential (which means I am an
       org admin or member of a team with access to all of the above).
     - I have permission explicitly granted to check/deploy with the inventory
       and project.

    This does not mean I would be able to launch a job from the template or
    edit the template.
    '''

    model = JobTemplate

    def get_queryset(self):
        qs = self.model.objects.distinct()
        qs = qs.select_related('created_by', 'modified_by', 'inventory', 'project',
                               'credential', 'cloud_credential', 'next_schedule')
        if self.user.is_superuser:
            return qs
        credential_ids = self.user.get_queryset(Credential)
        inventory_ids = self.user.get_queryset(Inventory)
        base_qs = qs.filter(
            Q(credential_id__in=credential_ids) | Q(credential__isnull=True),
            Q(cloud_credential_id__in=credential_ids) | Q(cloud_credential__isnull=True),
        )
        org_admin_ids = base_qs.filter(
            Q(project__deprecated_organizations__deprecated_admins__in=[self.user]) |
            (Q(project__isnull=True) & Q(job_type=PERM_INVENTORY_SCAN) & Q(inventory__organization__deprecated_admins__in=[self.user]))
        )

        allowed_deploy = [PERM_JOBTEMPLATE_CREATE, PERM_INVENTORY_DEPLOY]
        allowed_check = [PERM_JOBTEMPLATE_CREATE, PERM_INVENTORY_DEPLOY, PERM_INVENTORY_CHECK]

        team_ids = Team.objects.filter(deprecated_users__in=[self.user])

        deploy_permissions_ids = Permission.objects.filter(
            Q(user=self.user) | Q(team_id__in=team_ids),
            permission_type__in=allowed_deploy,
        )
        check_permissions_ids = Permission.objects.filter(
            Q(user=self.user) | Q(team_id__in=team_ids),
            permission_type__in=allowed_check,
        )

        perm_deploy_ids = base_qs.filter(
            job_type=PERM_INVENTORY_DEPLOY,
            inventory__permissions__in=deploy_permissions_ids,
            project__permissions__in=deploy_permissions_ids,
            inventory__permissions__pk=F('project__permissions__pk'),
            inventory_id__in=inventory_ids,
        )

        perm_check_ids = base_qs.filter(
            job_type=PERM_INVENTORY_CHECK,
            inventory__permissions__in=check_permissions_ids,
            project__permissions__in=check_permissions_ids,
            inventory__permissions__pk=F('project__permissions__pk'),
            inventory_id__in=inventory_ids,
        )

        return base_qs.filter(
            Q(id__in=org_admin_ids) |
            Q(id__in=perm_deploy_ids) |
            Q(id__in=perm_check_ids)
        )

    def can_read(self, obj):
        # you can only see the job templates that you have permission to launch.
        return self.can_start(obj, validate_license=False)

    def can_add(self, data):
        '''
        a user can create a job template if they are a superuser, an org admin
        of any org that the project is a member, or if they have user or team
        based permissions tying the project to the inventory source for the
        given action as well as the 'create' deploy permission.
        Users who are able to create deploy jobs can also run normal and check (dry run) jobs.
        '''
        if not data or '_method' in data:  # So the browseable API will work?
            return True

        if 'job_type' in data and data['job_type'] == PERM_INVENTORY_SCAN:
            self.check_license(feature='system_tracking')

        if 'survey_enabled' in data and data['survey_enabled']:
            self.check_license(feature='surveys')

        if self.user.is_superuser:
            return True

        # If a credential is provided, the user should have read access to it.
        credential_pk = get_pk_from_dict(data, 'credential')
        if credential_pk:
            credential = get_object_or_400(Credential, pk=credential_pk)
            if not check_user_access(self.user, Credential, 'read', credential):
                return False

        # If a cloud credential is provided, the user should have read access.
        cloud_credential_pk = get_pk_from_dict(data, 'cloud_credential')
        if cloud_credential_pk:
            cloud_credential = get_object_or_400(Credential,
                                                 pk=cloud_credential_pk)
            if not check_user_access(self.user, Credential, 'read', cloud_credential):
                return False

        # Check that the given inventory ID is valid.
        inventory_pk = get_pk_from_dict(data, 'inventory')
        inventory = Inventory.objects.filter(id=inventory_pk)
        if not inventory.exists():
            return False # Does this make sense?  Maybe should check read access

        project_pk = get_pk_from_dict(data, 'project')
        if 'job_type' in data and data['job_type'] == PERM_INVENTORY_SCAN:
            if not project_pk and check_user_access(self.user, Organization, 'change', inventory[0].organization, None):
                return True
            elif not check_user_access(self.user, Organization, "change", inventory[0].organization, None):
                return False
        # If the user has admin access to the project (as an org admin), should
        # be able to proceed without additional checks.
        project = get_object_or_400(Project, pk=project_pk)
        if check_user_access(self.user, Project, 'admin', project, None):
            return True

        # Otherwise, check for explicitly granted permissions to create job templates
        # for the project and inventory.
        permission_qs = Permission.objects.filter(
            Q(user=self.user) | Q(team__deprecated_users__in=[self.user]),
            inventory=inventory,
            project=project,
            #permission_type__in=[PERM_INVENTORY_CHECK, PERM_INVENTORY_DEPLOY],
            permission_type=PERM_JOBTEMPLATE_CREATE,
        )
        if permission_qs.exists():
            return True
        return False

        # job_type = data.get('job_type', None)

        # for perm in permission_qs:
        #     # if you have run permissions, you can also create check jobs
        #     if job_type == PERM_INVENTORY_CHECK:
        #         has_perm = True
        #     # you need explicit run permissions to make run jobs
        #     elif job_type == PERM_INVENTORY_DEPLOY and perm.permission_type == PERM_INVENTORY_DEPLOY:
        #         has_perm = True
        # if not has_perm:
        #     return False
        # return True

        # shouldn't really matter with permissions given, but make sure the user
        # is also currently on the team in case they were added a per-user permission and then removed
        # from the project.
        #if not project.teams.filter(users__in=[self.user]).count():
        #    return False

    def can_start(self, obj, validate_license=True):
        # Check license.
        if validate_license:
            self.check_license()
            if obj.job_type == PERM_INVENTORY_SCAN:
                self.check_license(feature='system_tracking')
            if obj.survey_enabled:
                self.check_license(feature='surveys')

        # Super users can start any job
        if self.user.is_superuser:
            return True
        # Check to make sure both the inventory and project exist
        if obj.inventory is None:
            return False
        if obj.job_type == PERM_INVENTORY_SCAN:
            if obj.project is None and check_user_access(self.user, Organization, 'change', obj.inventory.organization, None):
                return True
            if not check_user_access(self.user, Organization, 'change', obj.inventory.organization, None):
                return False
        if obj.project is None:
            return False
        # If the user has admin access to the project they can start a job
        if check_user_access(self.user, Project, 'admin', obj.project, None):
            return True

        # Otherwise check for explicitly granted permissions
        permission_qs = Permission.objects.filter(
            Q(user=self.user) | Q(team__deprecated_users__in=[self.user]),
            inventory=obj.inventory,
            project=obj.project,
            permission_type__in=[PERM_JOBTEMPLATE_CREATE, PERM_INVENTORY_CHECK, PERM_INVENTORY_DEPLOY],
        )

        has_perm = False
        for perm in permission_qs:
            # If you have job template create permission that implies both CHECK and DEPLOY
            # If you have DEPLOY permissions you can run both CHECK and DEPLOY
            if perm.permission_type in [PERM_JOBTEMPLATE_CREATE, PERM_INVENTORY_DEPLOY] and \
               obj.job_type == PERM_INVENTORY_DEPLOY:
                has_perm = True
            # If you only have CHECK permission then you can only run CHECK
            if perm.permission_type in [PERM_JOBTEMPLATE_CREATE, PERM_INVENTORY_DEPLOY, PERM_INVENTORY_CHECK] and \
               obj.job_type == PERM_INVENTORY_CHECK:
                has_perm = True

        return \
            has_perm and \
            check_user_access(self.user, Inventory, 'read', obj.inventory) and \
            check_user_access(self.user, Project, 'read', obj.project)

    def can_change(self, obj, data):
        data_for_change = data
        if data is not None:
            data_for_change = dict(data)
            for required_field in ('credential', 'cloud_credential', 'inventory', 'project'):
                required_obj = getattr(obj, required_field, None)
                if required_field not in data_for_change and required_obj is not None:
                    data_for_change[required_field] = required_obj.pk
        return self.can_read(obj) and self.can_add(data_for_change)

    def can_delete(self, obj):
        add_obj = dict(credential=obj.credential.id if obj.credential is not None else None,
                       cloud_credential=obj.cloud_credential.id if obj.cloud_credential is not None else None,
                       inventory=obj.inventory.id if obj.inventory is not None else None,
                       project=obj.project.id if obj.project is not None else None,
                       job_type=obj.job_type)
        return self.can_add(add_obj)


class JobAccess(BaseAccess):

    model = Job

    def get_queryset(self):
        qs = self.model.objects.distinct()
        qs = qs.select_related('created_by', 'modified_by', 'job_template', 'inventory',
                               'project', 'credential', 'cloud_credential', 'job_template')
        qs = qs.prefetch_related('unified_job_template')
        if self.user.is_superuser:
            return qs
        credential_ids = self.user.get_queryset(Credential)
        base_qs = qs.filter(
            credential_id__in=credential_ids,
        )
        org_admin_ids = base_qs.filter(
            Q(project__deprecated_organizations__deprecated_admins__in=[self.user]) |
            (Q(project__isnull=True) & Q(job_type=PERM_INVENTORY_SCAN) & Q(inventory__organization__deprecated_admins__in=[self.user]))
        )

        allowed_deploy = [PERM_JOBTEMPLATE_CREATE, PERM_INVENTORY_DEPLOY]
        allowed_check = [PERM_JOBTEMPLATE_CREATE, PERM_INVENTORY_DEPLOY, PERM_INVENTORY_CHECK]
        team_ids = Team.objects.filter(deprecated_users__in=[self.user])

        deploy_permissions_ids = Permission.objects.filter(
            Q(user=self.user) | Q(team__in=team_ids),
            permission_type__in=allowed_deploy,
        )
        check_permissions_ids = Permission.objects.filter(
            Q(user=self.user) | Q(team__in=team_ids),
            permission_type__in=allowed_check,
        )

        perm_deploy_ids = base_qs.filter(
            job_type=PERM_INVENTORY_DEPLOY,
            inventory__permissions__in=deploy_permissions_ids,
            project__permissions__in=deploy_permissions_ids,
            inventory__permissions__pk=F('project__permissions__pk'),
        )

        perm_check_ids = base_qs.filter(
            job_type=PERM_INVENTORY_CHECK,
            inventory__permissions__in=check_permissions_ids,
            project__permissions__in=check_permissions_ids,
            inventory__permissions__pk=F('project__permissions__pk'),
        )

        return base_qs.filter(
            Q(id__in=org_admin_ids) |
            Q(id__in=perm_deploy_ids) |
            Q(id__in=perm_check_ids)
        )

    def can_add(self, data):
        if not data or '_method' in data:  # So the browseable API will work?
            return True
        if not self.user.is_superuser:
            return False


        add_data = dict(data.items())

        # If a job template is provided, the user should have read access to it.
        job_template_pk = get_pk_from_dict(data, 'job_template')
        if job_template_pk:
            job_template = get_object_or_400(JobTemplate, pk=job_template_pk)
            add_data.setdefault('inventory', job_template.inventory.pk)
            add_data.setdefault('project', job_template.project.pk)
            add_data.setdefault('job_type', job_template.job_type)
            if job_template.credential:
                add_data.setdefault('credential', job_template.credential.pk)
        else:
            job_template = None

        return True

    def can_change(self, obj, data):
        return obj.status == 'new' and self.can_read(obj) and self.can_add(data)

    def can_delete(self, obj):
        return self.can_read(obj)

    def can_start(self, obj):
        self.check_license()

        # A super user can relaunch a job
        if self.user.is_superuser:
            return True
        # If a user can launch the job template then they can relaunch a job from that
        # job template
        has_perm = False
        if obj.job_template is not None and check_user_access(self.user, JobTemplate, 'start', obj.job_template):
            has_perm = True
        dep_access_inventory = check_user_access(self.user, Inventory, 'read', obj.inventory)
        dep_access_project = obj.project is None or check_user_access(self.user, Project, 'read', obj.project)
        return self.can_read(obj) and dep_access_inventory and dep_access_project and has_perm

    def can_cancel(self, obj):
        return self.can_read(obj) and obj.can_cancel


class SystemJobTemplateAccess(BaseAccess):
    '''
    I can only see/manage System Job Templates if I'm a super user
    '''

    model = SystemJobTemplate

    def can_start(self, obj):
        return self.can_read(obj)


class SystemJobAccess(BaseAccess):
    '''
    I can only see manage System Jobs if I'm a super user
    '''
    model = SystemJob


class AdHocCommandAccess(BaseAccess):
    '''
    I can only see/run ad hoc commands when:
    - I am a superuser.
    - I am an org admin and have permission to read the credential.
    - I am a normal user with a user/team permission that has at least read
      permission on the inventory and the run_ad_hoc_commands flag set, and I
      can read the credential.
    '''
    model = AdHocCommand

    def get_queryset(self):
        qs = self.model.objects.distinct()
        qs = qs.select_related('created_by', 'modified_by', 'inventory',
                               'credential')
        if self.user.is_superuser:
            return qs

        credential_ids = set(self.user.get_queryset(Credential).values_list('id', flat=True))
        team_ids = set(Team.objects.filter(deprecated_users__in=[self.user]).values_list('id', flat=True))

        permission_ids = set(Permission.objects.filter(
            Q(user=self.user) | Q(team__in=team_ids),
            permission_type__in=PERMISSION_TYPES_ALLOWING_INVENTORY_READ,
            run_ad_hoc_commands=True,
        ).values_list('id', flat=True))

        inventory_qs = self.user.get_queryset(Inventory)
        inventory_qs = inventory_qs.filter(Q(permissions__in=permission_ids) | Q(organization__deprecated_admins__in=[self.user]))
        inventory_ids = set(inventory_qs.values_list('id', flat=True))

        qs = qs.filter(
            credential_id__in=credential_ids,
            inventory_id__in=inventory_ids,
        )
        return qs

    def can_add(self, data):
        if not data or '_method' in data:  # So the browseable API will work?
            return True

        self.check_license()

        # If a credential is provided, the user should have read access to it.
        credential_pk = get_pk_from_dict(data, 'credential')
        if credential_pk:
            credential = get_object_or_400(Credential, pk=credential_pk)
            if not check_user_access(self.user, Credential, 'read', credential):
                return False

        # Check that the user has the run ad hoc command permission on the
        # given inventory.
        inventory_pk = get_pk_from_dict(data, 'inventory')
        if inventory_pk:
            inventory = get_object_or_400(Inventory, pk=inventory_pk)
            if not check_user_access(self.user, Inventory, 'run_ad_hoc_commands', inventory):
                return False

        return True

    def can_change(self, obj, data):
        return False

    def can_delete(self, obj):
        return self.can_read(obj)

    def can_start(self, obj):
        return self.can_add({
            'credential': obj.credential_id,
            'inventory': obj.inventory_id,
        })

    def can_cancel(self, obj):
        return self.can_read(obj) and obj.can_cancel


class AdHocCommandEventAccess(BaseAccess):
    '''
    I can see ad hoc command event records whenever I can read both ad hoc
    command and host.
    '''

    model = AdHocCommandEvent

    def get_queryset(self):
        qs = self.model.objects.distinct()
        qs = qs.select_related('ad_hoc_command', 'host')

        if self.user.is_superuser:
            return qs
        ad_hoc_command_qs = self.user.get_queryset(AdHocCommand)
        host_qs = self.user.get_queryset(Host)
        qs = qs.filter(Q(host__isnull=True) | Q(host__in=host_qs),
                       ad_hoc_command__in=ad_hoc_command_qs)
        return qs

    def can_add(self, data):
        return False

    def can_change(self, obj, data):
        return False

    def can_delete(self, obj):
        return False


class JobHostSummaryAccess(BaseAccess):
    '''
    I can see job/host summary records whenever I can read both job and host.
    '''

    model = JobHostSummary

    def get_queryset(self):
        qs = self.model.objects.distinct()
        qs = qs.select_related('job', 'job__job_template', 'host')
        if self.user.is_superuser:
            return qs
        job_qs = self.user.get_queryset(Job)
        host_qs = self.user.get_queryset(Host)
        return qs.filter(job__in=job_qs, host__in=host_qs)

    def can_add(self, data):
        return False

    def can_change(self, obj, data):
        return False

    def can_delete(self, obj):
        return False


class JobEventAccess(BaseAccess):
    '''
    I can see job event records whenever I can read both job and host.
    '''

    model = JobEvent

    def get_queryset(self):
        qs = self.model.objects.distinct()
        qs = qs.select_related('job', 'job__job_template', 'host', 'parent')
        qs = qs.prefetch_related('hosts', 'children')

        # Filter certain "internal" events generated by async polling.
        qs = qs.exclude(event__in=('runner_on_ok', 'runner_on_failed'),
                        event_data__icontains='"ansible_job_id": "',
                        event_data__contains='"module_name": "async_status"')

        if self.user.is_superuser:
            return qs
        job_qs = self.user.get_queryset(Job)
        host_qs = self.user.get_queryset(Host)
        qs = qs.filter(Q(host__isnull=True) | Q(host__in=host_qs),
                       job__in=job_qs)
        return qs

    def can_add(self, data):
        return False

    def can_change(self, obj, data):
        return False

    def can_delete(self, obj):
        return False


class UnifiedJobTemplateAccess(BaseAccess):
    '''
    I can see a unified job template whenever I can see the same project,
    inventory source or job template.  Unified job templates do not include
    projects without SCM configured or inventory sources without a cloud
    source.
    '''

    model = UnifiedJobTemplate

    def get_queryset(self):
        qs = self.model.objects.distinct()
        project_qs = self.user.get_queryset(Project).filter(scm_type__in=[s[0] for s in Project.SCM_TYPE_CHOICES])
        inventory_source_qs = self.user.get_queryset(InventorySource).filter(source__in=CLOUD_INVENTORY_SOURCES)
        job_template_qs = self.user.get_queryset(JobTemplate)
        qs = qs.filter(Q(Project___in=project_qs) |
                       Q(InventorySource___in=inventory_source_qs) |
                       Q(JobTemplate___in=job_template_qs))
        qs = qs.select_related(
            'created_by',
            'modified_by',
            #'project',
            #'inventory',
            #'credential',
            #'cloud_credential',
            'next_schedule',
            'last_job',
            'current_job',
        )
        # FIXME: Figure out how to do select/prefetch on related project/inventory/credential/cloud_credential.
        return qs


class UnifiedJobAccess(BaseAccess):
    '''
    I can see a unified job whenever I can see the same project update,
    inventory update or job.
    '''

    model = UnifiedJob

    def get_queryset(self):
        qs = self.model.objects.distinct()
        project_update_qs = self.user.get_queryset(ProjectUpdate)
        inventory_update_qs = self.user.get_queryset(InventoryUpdate).filter(source__in=CLOUD_INVENTORY_SOURCES)
        job_qs = self.user.get_queryset(Job)
        ad_hoc_command_qs = self.user.get_queryset(AdHocCommand)
        system_job_qs = self.user.get_queryset(SystemJob)
        qs = qs.filter(Q(ProjectUpdate___in=project_update_qs) |
                       Q(InventoryUpdate___in=inventory_update_qs) |
                       Q(Job___in=job_qs) |
                       Q(AdHocCommand___in=ad_hoc_command_qs) |
                       Q(SystemJob___in=system_job_qs))
        qs = qs.select_related(
            'created_by',
            'modified_by',
            #'project',
            #'inventory',
            #'credential',
            #'project___credential',
            #'inventory_source___credential',
            #'inventory_source___inventory',
            #'job_template___inventory',
            #'job_template___project',
            #'job_template___credential',
            #'job_template___cloud_credential',
        )
        qs = qs.prefetch_related('unified_job_template')
        # FIXME: Figure out how to do select/prefetch on related project/inventory/credential/cloud_credential.
        return qs


class ScheduleAccess(BaseAccess):
    '''
    I can see a schedule if I can see it's related unified job, I can create them or update them if I have write access
    '''

    model = Schedule

    def get_queryset(self):
        qs = self.model.objects.distinct()
        qs = qs.select_related('created_by', 'modified_by')
        qs = qs.prefetch_related('unified_job_template')
        if self.user.is_superuser:
            return qs
        job_template_qs = self.user.get_queryset(JobTemplate)
        inventory_source_qs = self.user.get_queryset(InventorySource)
        project_qs = self.user.get_queryset(Project)
        unified_qs = UnifiedJobTemplate.objects.filter(jobtemplate__in=job_template_qs) | \
            UnifiedJobTemplate.objects.filter(Q(project__in=project_qs)) | \
            UnifiedJobTemplate.objects.filter(Q(inventorysource__in=inventory_source_qs))
        return qs.filter(unified_job_template__in=unified_qs)

    def can_read(self, obj):
        if self.user.is_superuser:
            return True
        if obj and obj.unified_job_template:
            job_class = obj.unified_job_template
            return check_user_access(self.user, type(job_class), 'read', obj.unified_job_template)
        else:
            return False

    def can_add(self, data):
        if self.user.is_superuser:
            return True
        pk = get_pk_from_dict(data, 'unified_job_template')
        obj = get_object_or_400(UnifiedJobTemplate, pk=pk)
        if obj:
            return check_user_access(self.user, type(obj), 'change', obj, None)
        else:
            return False

    def can_change(self, obj, data):
        if self.user.is_superuser:
            return True
        if obj and obj.unified_job_template:
            job_class = obj.unified_job_template
            return check_user_access(self.user, type(job_class), 'change', job_class, None)
        else:
            return False

    def can_delete(self, obj):
        if self.user.is_superuser:
            return True
        if obj and obj.unified_job_template:
            job_class = obj.unified_job_template
            return check_user_access(self.user, type(job_class), 'change', job_class, None)
        else:
            return False


class ActivityStreamAccess(BaseAccess):
    '''
    I can see activity stream events only when I have permission on all objects included in the event
    '''

    model = ActivityStream

    def get_queryset(self):
        qs = self.model.objects.distinct()
        qs = qs.select_related('actor')
        qs = qs.prefetch_related('organization', 'user', 'inventory', 'host', 'group', 'inventory_source',
                                 'inventory_update', 'credential', 'team', 'project', 'project_update',
                                 'permission', 'job_template', 'job')
        if self.user.is_superuser:
            return qs

        user_admin_orgs = self.user.deprecated_admin_of_organizations.all()
        user_orgs = self.user.deprecated_organizations.all()

        #Organization filter
        qs = qs.filter(Q(organization__deprecated_admins__in=[self.user]) | Q(organization__deprecated_users__in=[self.user]))

        #User filter
        qs = qs.filter(Q(user__pk=self.user.pk) |
                       Q(user__deprecated_organizations__in=user_admin_orgs) |
                       Q(user__deprecated_organizations__in=user_orgs))

        #Inventory filter
        inventory_qs = self.user.get_queryset(Inventory)
        qs.filter(inventory__in=inventory_qs)

        #Host filter
        qs.filter(host__inventory__in=inventory_qs)

        #Group filter
        qs.filter(group__inventory__in=inventory_qs)

        #Inventory Source Filter
        qs.filter(Q(inventory_source__inventory__in=inventory_qs) |
                  Q(inventory_source__group__inventory__in=inventory_qs))

        #Inventory Update Filter
        qs.filter(Q(inventory_update__inventory_source__inventory__in=inventory_qs) |
                  Q(inventory_update__inventory_source__group__inventory__in=inventory_qs))

        #Credential Update Filter
        qs.filter(Q(credential__user=self.user) |
                  Q(credential__user__deprecated_organizations__in=user_admin_orgs) |
                  Q(credential__user__deprecated_admin_of_organizations__in=user_admin_orgs) |
                  Q(credential__team__organization__in=user_admin_orgs) |
                  Q(credential__team__deprecated_users__in=[self.user]))

        #Team Filter
        qs.filter(Q(team__organization__deprecated_admins__in=[self.user]) |
                  Q(team__deprecated_users__in=[self.user]))

        #Project Filter
        project_qs = self.user.get_queryset(Project)
        qs.filter(project__in=project_qs)

        #Project Update Filter
        qs.filter(project_update__project__in=project_qs)

        #Permission Filter
        permission_qs = self.user.get_queryset(Permission)
        qs.filter(permission__in=permission_qs)

        #Job Template Filter
        jobtemplate_qs = self.user.get_queryset(JobTemplate)
        qs.filter(job_template__in=jobtemplate_qs)

        #Job Filter
        job_qs = self.user.get_queryset(Job)
        qs.filter(job__in=job_qs)

        # Ad Hoc Command Filter
        ad_hoc_command_qs = self.user.get_queryset(AdHocCommand)
        qs.filter(ad_hoc_command__in=ad_hoc_command_qs)

        # organization_qs = self.user.get_queryset(Organization)
        # user_qs = self.user.get_queryset(User)
        # inventory_qs = self.user.get_queryset(Inventory)
        # host_qs = self.user.get_queryset(Host)
        # group_qs = self.user.get_queryset(Group)
        # inventory_source_qs = self.user.get_queryset(InventorySource)
        # inventory_update_qs = self.user.get_queryset(InventoryUpdate)
        # credential_qs = self.user.get_queryset(Credential)
        # team_qs = self.user.get_queryset(Team)
        # project_qs = self.user.get_queryset(Project)
        # project_update_qs = self.user.get_queryset(ProjectUpdate)
        # permission_qs = self.user.get_queryset(Permission)
        # job_template_qs = self.user.get_queryset(JobTemplate)
        # job_qs = self.user.get_queryset(Job)
        # qs = qs.filter(Q(organization__in=organization_qs) |
        #                Q(user__in=user_qs) |
        #                Q(inventory__in=inventory_qs) |
        #                Q(host__in=host_qs) |
        #                Q(group__in=group_qs) |
        #                Q(inventory_source__in=inventory_source_qs) |
        #                Q(credential__in=credential_qs) |
        #                Q(team__in=team_qs) |
        #                Q(project__in=project_qs) |
        #                Q(project_update__in=project_update_qs) |
        #                Q(permission__in=permission_qs) |
        #                Q(job_template__in=job_template_qs) |
        #                Q(job__in=job_qs))
        return qs

    def can_add(self, data):
        return False

    def can_change(self, obj, data):
        return False

    def can_delete(self, obj):
        return False


class CustomInventoryScriptAccess(BaseAccess):

    model = CustomInventoryScript

    def get_queryset(self):
        qs = self.model.objects.distinct()
        if not self.user.is_superuser:
            qs = qs.filter(Q(organization__deprecated_admins__in=[self.user]) | Q(organization__deprecated_users__in=[self.user]))
        return qs

    def can_read(self, obj):
        if self.user.is_superuser:
            return True
        return bool(obj.organization in self.user.deprecated_organizations.all() or obj.organization in self.user.deprecated_admin_of_organizations.all())

    def can_add(self, data):
        if self.user.is_superuser:
            return True
        return False

    def can_change(self, obj, data):
        if self.user.is_superuser:
            return True
        return False

    def can_delete(self, obj):
        if self.user.is_superuser:
            return True
        return False


register_access(User, UserAccess)
register_access(Organization, OrganizationAccess)
register_access(Inventory, InventoryAccess)
register_access(Host, HostAccess)
register_access(Group, GroupAccess)
register_access(InventorySource, InventorySourceAccess)
register_access(InventoryUpdate, InventoryUpdateAccess)
register_access(Credential, CredentialAccess)
register_access(Team, TeamAccess)
register_access(Project, ProjectAccess)
register_access(ProjectUpdate, ProjectUpdateAccess)
register_access(Permission, PermissionAccess)
register_access(JobTemplate, JobTemplateAccess)
register_access(Job, JobAccess)
register_access(JobHostSummary, JobHostSummaryAccess)
register_access(JobEvent, JobEventAccess)
register_access(SystemJobTemplate, SystemJobTemplateAccess)
register_access(SystemJob, SystemJobAccess)
register_access(AdHocCommand, AdHocCommandAccess)
register_access(AdHocCommandEvent, AdHocCommandEventAccess)
register_access(Schedule, ScheduleAccess)
register_access(UnifiedJobTemplate, UnifiedJobTemplateAccess)
register_access(UnifiedJob, UnifiedJobAccess)
register_access(ActivityStream, ActivityStreamAccess)
register_access(CustomInventoryScript, CustomInventoryScriptAccess)
