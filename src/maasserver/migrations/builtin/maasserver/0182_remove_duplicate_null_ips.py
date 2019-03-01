# -*- coding: utf-8 -*-
# Generated by Django 1.11.11 on 2019-02-27 10:48
from __future__ import unicode_literals

from django.db import migrations

# Try to keep one DISCOVERED (6) staticipaddress record that has a NULL
# ip field for each interface and subnet.
REMOVE_NULL_IPS_TEMPLATE = """
-- This deletes the links between the interfaces and the redundant,
-- NULL ip records, and keeps track of which staticipaddress records the
-- delete links were pointing at.
WITH deletions AS (
    DELETE FROM maasserver_interface_ip_addresses
    WHERE staticipaddress_id IN (
        SELECT maasserver_staticipaddress.id
        FROM maasserver_staticipaddress
        JOIN maasserver_interface_ip_addresses
            ON maasserver_interface_ip_addresses.staticipaddress_id =
                maasserver_staticipaddress.id
        WHERE maasserver_staticipaddress.ip IS NULL
            AND maasserver_staticipaddress.alloc_type = 6
            AND maasserver_staticipaddress.id NOT IN (
                SELECT {}(inner_interface_ip_addresses.staticipaddress_id)
                FROM maasserver_interface_ip_addresses
                    AS inner_interface_ip_addresses
                JOIN maasserver_staticipaddress AS inner_staticipaddress
                    ON inner_staticipaddress.id =
                        inner_interface_ip_addresses.staticipaddress_id
                WHERE inner_staticipaddress.alloc_type = 6
                    AND inner_staticipaddress.ip IS NULL
                GROUP BY inner_interface_ip_addresses.interface_id,
                         inner_staticipaddress.subnet_id
            )
            -- In theory a BMC could be linked to an address that we're
            -- removing, so just exclude them to avoid having the patch
            -- break. In practice, it's very unlikely that this would
            -- happen, but it's better to be defensive and delete less
            -- than to break.
            AND maasserver_staticipaddress.id NOT IN (
                SELECT maasserver_bmc.ip_address_id
                FROM maasserver_bmc
                WHERE maasserver_bmc.ip_address_id IS NOT NULL
            )
        )
    RETURNING staticipaddress_id)

-- The deletes all the actual staticipaddress records for which the links
-- were deleted. We need to keep track of the deleted IDs like this,
-- since cascading deletes aren't set up in the database.
DELETE FROM maasserver_staticipaddress
USING deletions
WHERE id = deletions.staticipaddress_id;

"""
REMOVE_NULL_IPS_MIN = REMOVE_NULL_IPS_TEMPLATE.format('min')
REMOVE_NULL_IPS_MAX = REMOVE_NULL_IPS_TEMPLATE.format('max')


class Migration(migrations.Migration):

    dependencies = [
        ('maasserver', '0181_packagerepository_disable_sources'),
    ]

    # We run the query first using max() and then min(). The reason for
    # this is that if multiple interfaces link to the same
    # staticipaddress record, they might still have more than one after
    # the query has finished. Running it twice like this reduces the
    # risk of that happening.
    operations = [
        migrations.RunSQL(REMOVE_NULL_IPS_MAX),
        migrations.RunSQL(REMOVE_NULL_IPS_MIN),
    ]