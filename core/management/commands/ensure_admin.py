from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model


class Command(BaseCommand):
    help = "Ensure admin user with username 'EpicDub' and password 'EpicDubStudio' exists."

    def handle(self, *args, **options):
        User = get_user_model()
        username = 'EpicDub'
        password = 'EpicDubStudio'
        email = 'admin@example.com'

        user, created = User.objects.get_or_create(username=username, defaults={
            'email': email,
            'is_staff': True,
            'is_superuser': True,
        })
        if created:
            user.set_password(password)
            user.save()
            self.stdout.write(self.style.SUCCESS("Admin user created."))
        else:
            changed = False
            if not user.is_staff:
                user.is_staff = True
                changed = True
            if not user.is_superuser:
                user.is_superuser = True
                changed = True
            user.set_password(password)
            changed = True
            if changed:
                user.save()
            self.stdout.write(self.style.SUCCESS("Admin user ensured/updated."))
