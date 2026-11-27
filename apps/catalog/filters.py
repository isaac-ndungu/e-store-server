"""Query filters for catalog listing endpoints.

Category filters expand the requested category to its whole subtree so a
parent landing page lists the products assigned to its child categories.
"""

import django_filters

from apps.catalog.models import Category, Product


class ProductFilter(django_filters.FilterSet):
    """Public product-list filters, including category descendant matching.

    ``in_stock`` is a convenience boolean over ``stock_status``: true hides
    out-of-stock rows, false leaves the list unchanged. ``price_min`` and
    ``price_max`` bound the base price; effective discount pricing stays on
    the price endpoint and is never recomputed here.
    """

    category = django_filters.NumberFilter(method="filter_category")
    in_stock = django_filters.BooleanFilter(method="filter_in_stock")
    price_min = django_filters.NumberFilter(field_name="price", lookup_expr="gte")
    price_max = django_filters.NumberFilter(field_name="price", lookup_expr="lte")

    class Meta:
        model = Product
        fields = ["brand", "is_featured", "product_type", "tax_class"]

    def filter_in_stock(self, queryset, name, value):
        """Hide out-of-stock products when the caller asks for in-stock only.

        Args:
            queryset (QuerySet): the product queryset being filtered.
            name (str): the filter field name.
            value (bool | None): the parsed boolean.

        Returns:
            QuerySet: the filtered queryset.
        """
        if not value:
            return queryset
        return queryset.exclude(stock_status="out_of_stock")

    def filter_category(self, queryset, name, value):
        """Filter products to a category and all of its descendants.

        Args:
            queryset (QuerySet): the product queryset being filtered.
            name (str): the filter field name.
            value (int | None): the requested category id.

        Returns:
            QuerySet: products assigned to the category subtree.
        """
        if value is None:
            return queryset
        category_ids = self._category_and_descendant_ids(value)
        return queryset.filter(category_id__in=category_ids)

    @staticmethod
    def _category_and_descendant_ids(category_id):
        """Collect a category id plus every nested child id below it.

        Args:
            category_id (int): the root category id to expand.

        Returns:
            list[int]: the requested id followed by each descendant id.
        """
        collected = [category_id]
        frontier = [category_id]
        seen = {category_id}
        while frontier:
            children = list(
                Category.objects.filter(parent_id__in=frontier).values_list(
                    "pk", flat=True
                )
            )
            frontier = [child for child in children if child not in seen]
            seen.update(frontier)
            collected.extend(frontier)
        return collected


class AdminProductFilter(ProductFilter):
    """Admin product-list filters, adding lifecycle and stock fields."""

    class Meta(ProductFilter.Meta):
        fields = ProductFilter.Meta.fields + [
            "is_active",
            "is_discontinued",
            "stock_status",
        ]
