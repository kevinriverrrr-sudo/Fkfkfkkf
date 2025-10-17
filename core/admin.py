from django.contrib import admin
from .models import Video, Comment, Reaction


@admin.register(Video)
class VideoAdmin(admin.ModelAdmin):
    list_display = ("id", "title", "created_by", "created_at")
    search_fields = ("title", "description")
    list_filter = ("created_at",)


@admin.register(Comment)
class CommentAdmin(admin.ModelAdmin):
    list_display = ("id", "video", "author", "parent", "created_at")
    search_fields = ("text",)
    list_filter = ("created_at",)


@admin.register(Reaction)
class ReactionAdmin(admin.ModelAdmin):
    list_display = ("id", "video", "user", "value", "created_at")
    list_filter = ("value", "created_at")

# Register your models here.
