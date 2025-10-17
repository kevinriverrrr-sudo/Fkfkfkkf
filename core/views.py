from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import UserCreationForm, AuthenticationForm
from django.http import HttpRequest, HttpResponse, JsonResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from .models import Video, Comment, Reaction


def index(request: HttpRequest) -> HttpResponse:
    videos = Video.objects.order_by('-created_at')
    return render(request, 'core/index.html', {"videos": videos})


def video_detail(request: HttpRequest, pk: int) -> HttpResponse:
    video = get_object_or_404(Video, pk=pk)
    comments = video.comments.select_related('author').prefetch_related('replies')
    like_count = video.reactions.filter(value=Reaction.LIKE).count()
    dislike_count = video.reactions.filter(value=Reaction.DISLIKE).count()
    return render(
        request,
        'core/video_detail.html',
        {
            "video": video,
            "comments": comments,
            "like_count": like_count,
            "dislike_count": dislike_count,
        },
    )


@login_required
def upload_video(request: HttpRequest) -> HttpResponse:
    if request.method == 'POST':
        title = request.POST.get('title', '').strip()
        description = request.POST.get('description', '').strip()
        vk_embed_url = request.POST.get('vk_embed_url', '').strip()
        file = request.FILES.get('file')

        if not title:
            return render(request, 'core/upload.html', {"error": "Введите заголовок"})

        video = Video.objects.create(
            title=title,
            description=description,
            vk_embed_url=vk_embed_url,
            file=file or None,
            created_by=request.user,
        )
        return redirect('video_detail', pk=video.pk)

    return render(request, 'core/upload.html')


@require_POST
@login_required
def add_comment(request: HttpRequest, pk: int) -> HttpResponse:
    video = get_object_or_404(Video, pk=pk)
    text = request.POST.get('text', '').strip()
    parent_id = request.POST.get('parent')
    parent = None
    if parent_id:
        parent = get_object_or_404(Comment, pk=parent_id, video=video)
    if not text:
        return redirect('video_detail', pk=pk)
    Comment.objects.create(video=video, author=request.user, parent=parent, text=text)
    return redirect('video_detail', pk=pk)


@require_POST
@login_required
def react(request: HttpRequest, pk: int) -> HttpResponse:
    video = get_object_or_404(Video, pk=pk)
    value = int(request.POST.get('value'))
    if value not in (Reaction.LIKE, Reaction.DISLIKE):
        return HttpResponseForbidden()
    reaction, _ = Reaction.objects.update_or_create(
        video=video, user=request.user, defaults={"value": value}
    )
    return redirect('video_detail', pk=pk)


def register_view(request: HttpRequest) -> HttpResponse:
    if request.method == 'POST':
        form = UserCreationForm(request.POST)
        if form.is_valid():
            form.save()
            username = form.cleaned_data.get('username')
            raw_password = form.cleaned_data.get('password1')
            user = authenticate(username=username, password=raw_password)
            if user:
                login(request, user)
                return redirect('index')
    else:
        form = UserCreationForm()
    return render(request, 'core/register.html', {"form": form})


def login_view(request: HttpRequest) -> HttpResponse:
    if request.method == 'POST':
        form = AuthenticationForm(request, data=request.POST)
        if form.is_valid():
            user = form.get_user()
            login(request, user)
            return redirect('index')
    else:
        form = AuthenticationForm(request)
    return render(request, 'core/login.html', {"form": form})


def logout_view(request: HttpRequest) -> HttpResponse:
    logout(request)
    return redirect('index')

# Create your views here.
