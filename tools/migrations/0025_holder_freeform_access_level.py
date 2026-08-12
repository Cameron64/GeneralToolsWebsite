"""ResourceHolder.accessLevel becomes free text, with the two facts the register
has to read across rows split out into their own booleans.

Why this is hand-written rather than the makemigrations default. The generated
migration was a single AlterField from IntegerField to CharField, which on every
backend coerces the stored integers to their own digits - every holder's access
level would have become the string "0", "1" or "4". The column has to be
introduced beside the old one, populated from it, and only then swapped in.

The four operations are therefore: add the new columns, translate, drop the old
column, and take its name. Splitting them across two migrations would work
equally well and is not done here because the intermediate state - a row whose
role is recorded twice, in two spellings - is not a state any deploy should be
able to stop in.
"""
from django.db import migrations, models


# The old rungs, by value. Written out rather than imported from the model,
# because the model no longer has them - and a migration that imports today's
# model is a migration that breaks the next time the model changes.
_ORDINARY = 0
_ADMIN = 1
_OWNER = 2
_PRIMARY_OWNER = 3
_UNCONFIRMED = 4

# Old rung -> (role name, canGrantAccess, ownsAccount).
#
# UNCONFIRMED maps to a BLANK role name, not to the words "Not confirmed yet".
# The old rung meant "nobody has checked what this person can do", and the row's
# own `confirmed` flag already carries that - so writing it into the role name
# would leave the register asserting a role that no service has ever used, which
# is exactly the kind of invented fact the blank is there to avoid.
_TRANSLATION = {
    _ORDINARY: ("Ordinary access", False, False),
    _ADMIN: ("Admin", True, False),
    _OWNER: ("Owner", True, True),
    _PRIMARY_OWNER: ("Primary owner", True, True),
    _UNCONFIRMED: ("", False, False),
}


def translateForward(apps, schema_editor):
    ResourceHolder = apps.get_model("tools", "ResourceHolder")
    for holder in ResourceHolder.objects.all().iterator():
        role, canGrant, owns = _TRANSLATION.get(holder.accessLevel, ("", False, False))
        holder.accessLevelRole = role
        holder.canGrantAccess = canGrant
        holder.ownsAccount = owns
        holder.save(update_fields=["accessLevelRole", "canGrantAccess", "ownsAccount"])


def translateBackward(apps, schema_editor):
    """Lossy, and deliberately so rather than refusing to run.

    Free text cannot be put back on a five-rung ladder: "Delegated user" was
    never a rung, and Owner and Primary owner both set the same two booleans so
    the distinction between them is already gone by the time we get here.
    Everything that owns the account comes back as Owner.

    A migration that cannot be reversed at all is worse for the one case this
    matters in - rolling a bad deploy back - than one that comes back with a
    coarser answer.
    """
    ResourceHolder = apps.get_model("tools", "ResourceHolder")
    for holder in ResourceHolder.objects.all().iterator():
        if holder.ownsAccount:
            level = _OWNER
        elif holder.canGrantAccess:
            level = _ADMIN
        elif holder.accessLevelRole.strip():
            level = _ORDINARY
        else:
            level = _UNCONFIRMED
        holder.accessLevel = level
        holder.save(update_fields=["accessLevel"])


class Migration(migrations.Migration):

    dependencies = [
        ("tools", "0024_resourcecredential_addedat_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="resourceholder",
            name="canGrantAccess",
            field=models.BooleanField(
                default=False,
                help_text="They can change what other people here are allowed to do, including removing somebody.",
            ),
        ),
        migrations.AddField(
            model_name="resourceholder",
            name="ownsAccount",
            field=models.BooleanField(
                default=False,
                help_text="They hold it at the top level: billing, deletion, and who the other owners are.",
            ),
        ),
        # Defined with the FINAL field spec, because RenameField below keeps the
        # definition and only changes the name - so anything different here would
        # show up as a pending change on the next makemigrations --check.
        migrations.AddField(
            model_name="resourceholder",
            name="accessLevelRole",
            field=models.CharField(
                blank=True,
                max_length=100,
                help_text=(
                    "What the service itself calls this person's role, in its own words. "
                    "Blank reads as not recorded, which is a true answer and better than a "
                    "guessed one."
                ),
            ),
        ),
        migrations.RunPython(translateForward, translateBackward),
        migrations.RemoveField(model_name="resourceholder", name="accessLevel"),
        migrations.RenameField(
            model_name="resourceholder",
            old_name="accessLevelRole",
            new_name="accessLevel",
        ),
    ]
