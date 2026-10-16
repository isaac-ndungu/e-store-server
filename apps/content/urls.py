"""URL routing for the content endpoints.

Mounted at ``/api/v1/`` from ``config/api_urls.py``. Public storefront reads
use slug-based resolution for pages and placement-based queries for banners;
admin CRUD paths use pk-based resolution. Only staff tokens may reach
the admin endpoints.
"""

from django.urls import path

from apps.content.feeds import BlogFeed, BlogFeedStylesheetView, BlogSitemapView
from apps.content.views import (
    BannerAdminCreateView,
    BannerAdminDeleteView,
    BannerAdminDetailView,
    BannerAdminListView,
    BannerAdminUpdateView,
    BannerStorefrontView,
    BlogCategoryAdminCreateView,
    BlogCategoryAdminDeleteView,
    BlogCategoryAdminListView,
    BlogCategoryAdminUpdateView,
    BlogCategoryListView,
    BlogPostAdminCreateView,
    BlogPostAdminDeleteView,
    BlogPostAdminDetailView,
    BlogPostAdminListView,
    BlogPostAdminUpdateView,
    BlogPostDetailView,
    BlogPostListView,
    BlogTagAdminCreateView,
    BlogTagAdminDeleteView,
    BlogTagListView,
    ContentPageAdminCreateView,
    ContentPageAdminDeleteView,
    ContentPageAdminDetailView,
    ContentPageAdminListView,
    ContentPageAdminUpdateView,
    ContentPageStorefrontView,
)

urlpatterns = [
    path(
        "content/pages/<slug:slug>/",
        ContentPageStorefrontView.as_view(),
        name="content-page-storefront",
    ),
    path(
        "content/banners/",
        BannerStorefrontView.as_view(),
        name="banner-storefront",
    ),
    path("blog/posts/", BlogPostListView.as_view(), name="blog-post-list"),
    path(
        "blog/posts/<slug:slug>/",
        BlogPostDetailView.as_view(),
        name="blog-post-detail",
    ),
    path(
        "blog/categories/",
        BlogCategoryListView.as_view(),
        name="blog-category-list",
    ),
    path("blog/tags/", BlogTagListView.as_view(), name="blog-tag-list"),
    path("blog/sitemap.xml", BlogSitemapView.as_view(), name="blog-sitemap"),
    path("blog/feed/", BlogFeed(), name="blog-feed"),
    path(
        "blog/feed.xsl",
        BlogFeedStylesheetView.as_view(),
        name="blog-feed-stylesheet",
    ),
    path(
        "blog/admin/posts/",
        BlogPostAdminListView.as_view(),
        name="blog-post-admin-list",
    ),
    path(
        "blog/admin/posts/create/",
        BlogPostAdminCreateView.as_view(),
        name="blog-post-admin-create",
    ),
    path(
        "blog/admin/posts/<int:post_id>/",
        BlogPostAdminDetailView.as_view(),
        name="blog-post-admin-detail",
    ),
    path(
        "blog/admin/posts/<int:post_id>/update/",
        BlogPostAdminUpdateView.as_view(),
        name="blog-post-admin-update",
    ),
    path(
        "blog/admin/posts/<int:post_id>/delete/",
        BlogPostAdminDeleteView.as_view(),
        name="blog-post-admin-delete",
    ),
    path(
        "blog/admin/categories/",
        BlogCategoryAdminListView.as_view(),
        name="blog-category-admin-list",
    ),
    path(
        "blog/admin/categories/create/",
        BlogCategoryAdminCreateView.as_view(),
        name="blog-category-admin-create",
    ),
    path(
        "blog/admin/categories/<int:category_id>/update/",
        BlogCategoryAdminUpdateView.as_view(),
        name="blog-category-admin-update",
    ),
    path(
        "blog/admin/categories/<int:category_id>/delete/",
        BlogCategoryAdminDeleteView.as_view(),
        name="blog-category-admin-delete",
    ),
    path(
        "blog/admin/tags/create/",
        BlogTagAdminCreateView.as_view(),
        name="blog-tag-admin-create",
    ),
    path(
        "blog/admin/tags/<int:tag_id>/delete/",
        BlogTagAdminDeleteView.as_view(),
        name="blog-tag-admin-delete",
    ),
    path(
        "content/admin/pages/",
        ContentPageAdminListView.as_view(),
        name="content-page-admin-list",
    ),
    path(
        "content/admin/pages/create/",
        ContentPageAdminCreateView.as_view(),
        name="content-page-admin-create",
    ),
    path(
        "content/admin/pages/<int:page_id>/",
        ContentPageAdminDetailView.as_view(),
        name="content-page-admin-detail",
    ),
    path(
        "content/admin/pages/<int:page_id>/update/",
        ContentPageAdminUpdateView.as_view(),
        name="content-page-admin-update",
    ),
    path(
        "content/admin/pages/<int:page_id>/delete/",
        ContentPageAdminDeleteView.as_view(),
        name="content-page-admin-delete",
    ),
    path(
        "content/admin/banners/",
        BannerAdminListView.as_view(),
        name="banner-admin-list",
    ),
    path(
        "content/admin/banners/create/",
        BannerAdminCreateView.as_view(),
        name="banner-admin-create",
    ),
    path(
        "content/admin/banners/<int:banner_id>/",
        BannerAdminDetailView.as_view(),
        name="banner-admin-detail",
    ),
    path(
        "content/admin/banners/<int:banner_id>/update/",
        BannerAdminUpdateView.as_view(),
        name="banner-admin-update",
    ),
    path(
        "content/admin/banners/<int:banner_id>/delete/",
        BannerAdminDeleteView.as_view(),
        name="banner-admin-delete",
    ),
]
