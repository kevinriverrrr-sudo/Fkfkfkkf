from django.db import models
from django.contrib.auth import get_user_model


User = get_user_model()


class Video(models.Model):
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    # Either VK player embed code or file upload
    vk_embed_url = models.URLField(blank=True, help_text="Ссылка на плеер VK (iframe src)")
    file = models.FileField(upload_to='videos/', blank=True)
    created_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='videos')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return self.title


class Comment(models.Model):
    video = models.ForeignKey(Video, on_delete=models.CASCADE, related_name='comments')
    author = models.ForeignKey(User, on_delete=models.CASCADE, related_name='comments')
    parent = models.ForeignKey('self', null=True, blank=True, on_delete=models.CASCADE, related_name='replies')
    text = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"{self.author}: {self.text[:30]}"


class Reaction(models.Model):
    LIKE = 1
    DISLIKE = -1
    VALUE_CHOICES = (
        (LIKE, 'Like'),
        (DISLIKE, 'Dislike'),
    )

    video = models.ForeignKey(Video, on_delete=models.CASCADE, related_name='reactions')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='reactions')
    value = models.SmallIntegerField(choices=VALUE_CHOICES)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('video', 'user')

# Create your models here.
